"""
sample_submission_gui.py — Sample submission form for IsoWorks.
Provides SampleSubmissionWindow for registering new sample submissions,
assigning sample types, and managing the submission load list via db_core.
"""
from __future__ import annotations
import sys
import os
from datetime import date, datetime
import pandas as pd
import logging
import pyodbc

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QRadioButton, QComboBox, QLineEdit,
    QPushButton, QTableView, QCheckBox, QLabel, QFormLayout,
    QMessageBox, QFileDialog, QSizePolicy, QStyledItemDelegate, QCompleter
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5.QtCore import Qt

# --- Shared Manager & SQLAlchemy ---
from db_core import db_manager
from sqlalchemy import text
from shared_utils import set_status
# --- Custo m Delegate for In-Table Combos ---
class ComboDelegate(QStyledItemDelegate):
    def __init__(self, parent, items_dict):
        super().__init__(parent)
        self.items_dict = items_dict 
        self.reverse_dict = {v: k for k, v in items_dict.items()}

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        sorted_items = sorted(self.items_dict.items(), key=lambda x: x[1])
        for data_val, display_text in sorted_items:
            combo.addItem(display_text, data_val)
        return combo

    def setEditorData(self, editor, index):
        current_data = index.data(Qt.UserRole)
        idx = editor.findData(current_data)
        if idx >= 0:
            editor.setCurrentIndex(idx)
        else:
            current_text = index.data(Qt.DisplayRole)
            idx = editor.findText(current_text)
            if idx >= 0: editor.setCurrentIndex(idx)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.DisplayRole)
        model.setData(index, editor.currentData(), Qt.UserRole)

    def update_items(self, new_items_dict):
        self.items_dict = new_items_dict
        self.reverse_dict = {v: k for k, v in new_items_dict.items()}

class SampleSubmissionWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # State
        self.user_field_mappings = {}
        self.user_field_origins = {}
        self.field_params_list = [] 
        
        # Data Maps
        self.country_map = {}
        self.sample_type_map = {}

        self.main_layout = QVBoxLayout(self)
        
        # --- Status Label (Top) ---
        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        # self.status_label.setFixedHeight(30)
        self.status_label.setWordWrap(True)
        self.main_layout.addWidget(self.status_label) 
                
        # --- Top Layout ---
        self.top_layout = QHBoxLayout()
        self.top_layout.addWidget(self._create_upload_mode_group())
        self.top_layout.addWidget(self._create_sample_type_group())
        
        # Status Label creation inside Upload Source group
        self.top_layout.addWidget(self._create_upload_source_group())
        
        self.top_layout.addStretch(1)
        
        # --- Right Side VBox (Save Button) ---
        vbox_right = QVBoxLayout()
        self.save_button = QPushButton("Save Submission")
        self.save_button.setMinimumHeight(40)
        self.save_button.setFixedWidth(200)
        self.save_button.setStyleSheet("""
            QPushButton {
                font-weight: bold; 
                background-color: #27ae60; 
                color: white; 
                padding: 5px 15px;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #2ecc71; }
            QPushButton:pressed { background-color: #219150; }
        """)
        vbox_right.addWidget(self.save_button)    
        vbox_right.addStretch() 
        self.top_layout.addLayout(vbox_right)
        
        self.main_layout.addLayout(self.top_layout)

        # --- Middle Layout ---
        self.middle_layout = QHBoxLayout()
        self.entry_layout = QVBoxLayout()
        self.entry_layout.addWidget(self._create_file_upload_group())
        self.entry_layout.addWidget(self._create_gnip_group())
        self.entry_layout.addWidget(self._create_trims_group())
        self.entry_layout.addStretch(1) 
        
        self.info_wrapper_layout = QVBoxLayout()
        self.info_layout = QHBoxLayout()
        self.info_layout.addWidget(self._create_submission_info_group())
        self.info_layout.addWidget(self._create_admin_info_group())
        self.info_wrapper_layout.addLayout(self.info_layout)
        self.info_wrapper_layout.addStretch(1) 
        
        self.middle_layout.addLayout(self.entry_layout, 1) 
        self.middle_layout.addLayout(self.info_wrapper_layout, 2)
        self.main_layout.addLayout(self.middle_layout)

        # --- Samples Table ---
        self.main_layout.addWidget(self._create_samples_table_group(), 1) 

        self.connect_signals()
        # Apply to Media Type
        self.enable_dynamic_search(self.cmb_media_type)        
        # Apply to Station (matches example "Ethiopia--Addis Ababa")
        self.enable_dynamic_search(self.cmb_station)        
        # Apply to Client (useful for finding "University of X")
        self.enable_dynamic_search(self.cmb_client)
        self.enable_dynamic_search(self.cmb_officer)
        self.enable_dynamic_search(self.cmb_payer)
                
        # --- Handle Startup ---
        try:
            db_manager.get_engine()
            self.setEnabled(True)
            self.load_initial_data()
            self._on_upload_mode_changed()
            self._on_upload_source_changed()
            set_status(self.status_label,"Ready.", "neutral")
        except Exception as e:
            logging.error(f"Database not ready: {e}")
            self.setEnabled(False)
            set_status(self.status_label,"Database connection failed.", "error")

    # --- Status Group Creation ---
    def _create_status_group(self):       
        group = QGroupBox("Status Label")
        group.setFixedWidth(200)
        layout = QHBoxLayout()
        self.status_label = QLabel("Ready ...")        
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("""
            QLabel {
                color: #555; 
                font-size: 13px; 
                border: 1px solid #ccc; 
                border-radius: 4px; 
                padding: 5px;
                background-color: #f9f9f9;
            }
        """)
        layout.addWidget(self.status_label, 1)
        group.setLayout(layout)
        return group

    # --- Config Helpers ---
    def get_config_val(self, token_name, default_value=-1.0):
        try:
            with db_manager.get_connection() as conn:
                sql = text("SELECT TokenValue FROM GlobalValue WHERE Token = :token")
                res = conn.execute(sql, {"token": token_name}).fetchone()
                return res[0] if res else default_value
        except Exception as e:
            logging.error(f"Config val error ({token_name}): {e}")
            return default_value

    def get_config_text(self, token_name, default_value=""):
        try:
            with db_manager.get_connection() as conn:
                sql = text("SELECT TokenValue FROM GlobalValue WHERE Token = :token")
                res = conn.execute(sql, {"token": token_name}).fetchone()
                return res[0] if res else default_value
        except Exception as e:
            logging.error(f"Config text error ({token_name}): {e}")
            return default_value

    # --- UI Groups ---
    def _create_upload_mode_group(self):
        group = QGroupBox("Upload Mode"); layout = QVBoxLayout()
        self.rb_new_submission = QRadioButton("Import a New Submission"); self.rb_append_submission = QRadioButton("Append Samples")
        self.cmb_available_projects = QComboBox(); self.cmb_available_projects.setEditable(True)
        layout.addWidget(self.rb_new_submission); layout.addWidget(self.rb_append_submission); layout.addWidget(self.cmb_available_projects)
        self.rb_new_submission.setChecked(True); self.cmb_available_projects.setEnabled(False)
        group.setLayout(layout); return group

    def _create_sample_type_group(self):
        group = QGroupBox("Sample Type"); layout = QVBoxLayout()
        self.rb_unknown_samples = QRadioButton("Unknown Samples"); self.rb_reference_samples = QRadioButton("References/Controls")
        self.rb_unknown_samples.setChecked(True); layout.addWidget(self.rb_unknown_samples); layout.addWidget(self.rb_reference_samples)
        group.setLayout(layout); return group

    def _create_upload_source_group(self):
        group = QGroupBox("Upload Source"); lay = QVBoxLayout()
        self.rb_lims_sheet = QRadioButton("LIMS Worksheet"); self.rb_manual_entry = QRadioButton("Manual Entry")
        self.rb_json = QRadioButton("JSON"); self.rb_gnip = QRadioButton("GNIP/GNIR"); self.rb_trims = QRadioButton("From TRIMS")
        self.rb_lims_sheet.setChecked(True)
        
        for rb in [self.rb_lims_sheet, self.rb_manual_entry, self.rb_json, self.rb_gnip, self.rb_trims]:
            lay.addWidget(rb); rb.toggled.connect(self._on_upload_source_changed)
        
        lay.addStretch()        
        group.setLayout(lay); return group

    def _create_file_upload_group(self):
        self.file_upload_group = QGroupBox("Submission File"); layout = QHBoxLayout()
        self.txt_upload_filename = QLineEdit(); self.txt_upload_filename.setPlaceholderText("Select a file...")
        self.btn_pick_file = QPushButton("..."); layout.addWidget(self.txt_upload_filename); layout.addWidget(self.btn_pick_file)
        self.file_upload_group.setLayout(layout); return self.file_upload_group

    def _create_gnip_group(self):
        self.gnip_group = QGroupBox("GNIP Station"); layout = QFormLayout()
        self.cmb_station = QComboBox(); self.cmb_station.setEditable(True)
        self.cmb_start_yr = QComboBox(); self.cmb_start_mo = QComboBox(); self.cmb_end_yr = QComboBox(); self.cmb_end_mo = QComboBox()
        self.btn_create_gnip_samples = QPushButton("Create Samples"); self.btn_reset_gnip = QPushButton("Reset")
        self._populate_gnip_combos(); layout.addRow("Station:", self.cmb_station)
        row1 = QHBoxLayout(); row1.addWidget(self.cmb_start_yr); row1.addWidget(self.cmb_start_mo); layout.addRow("From:", row1)
        row2 = QHBoxLayout(); row2.addWidget(self.cmb_end_yr); row2.addWidget(self.cmb_end_mo); layout.addRow("To:", row2)
        row3 = QHBoxLayout(); row3.addWidget(self.btn_create_gnip_samples); row3.addWidget(self.btn_reset_gnip); layout.addRow(row3)
        self.gnip_group.setLayout(layout); self.gnip_group.setVisible(False); return self.gnip_group

    def _create_trims_group(self):
        self.trims_group = QGroupBox("TRIMS Import"); layout = QHBoxLayout()
        self.cmb_trims_option = QComboBox(); self.cmb_trims_option.addItems(["Project #", "LSC RUN"])
        self.txt_trims_id = QLineEdit(); self.btn_trims_import = QPushButton("Import")
        layout.addWidget(self.cmb_trims_option); layout.addWidget(self.txt_trims_id); layout.addWidget(self.btn_trims_import)
        self.trims_group.setLayout(layout); self.trims_group.setVisible(False); return self.trims_group

    def _create_submission_info_group(self):
        group = QGroupBox("Submission Information"); group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum); layout = QFormLayout()
        self.txt_submission_id = QLineEdit(); self.txt_submission_id.setReadOnly(True)
        self.txt_submission_name = QLineEdit(); self.txt_sampling_site = QLineEdit()
        self.cmb_client = QComboBox(); self.cmb_client.setEditable(True)
        self.txt_submission_date = QLineEdit(); self.cmb_media_type = QComboBox()
        self.cmb_req_workflow = QComboBox(); self.chk_stage_to_tba = QCheckBox("Stage to TBA")
        layout.addRow("ID:", self.txt_submission_id); layout.addRow("Name*:", self.txt_submission_name)
        layout.addRow("Site*:", self.txt_sampling_site); layout.addRow("Client*:", self.cmb_client)
        layout.addRow("Date*:", self.txt_submission_date); layout.addRow("Media*:", self.cmb_media_type)
        layout.addRow("Workflow*:", self.cmb_req_workflow); layout.addRow(self.chk_stage_to_tba)
        group.setLayout(layout); return group

    def _create_admin_info_group(self):
        group = QGroupBox("Submission Administration"); group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum); layout = QFormLayout()
        self.cmb_officer = QComboBox(); self.cmb_officer.setEditable(True)
        self.cmb_payer = QComboBox(); self.cmb_payer.setEditable(True)
        self.txt_payer_ref = QLineEdit(); self.cmb_priority = QComboBox()
        self.txt_receiving_no = QLineEdit(); self.txt_invoice_no = QLineEdit()
        layout.addRow("Officer*:", self.cmb_officer); layout.addRow("Payer:", self.cmb_payer)
        layout.addRow("Ref:", self.txt_payer_ref); layout.addRow("Priority*:", self.cmb_priority)
        layout.addRow("Rcv No.:", self.txt_receiving_no); layout.addRow("Inv No.:", self.txt_invoice_no)
        group.setLayout(layout); return group

    def _create_samples_table_group(self):
        group = QGroupBox("Details of New Imported Samples")
        layout = QVBoxLayout()
        
        # --- Mapping Container (Right Aligned) ---
        self.mapping_container = QWidget()
        map_layout = QHBoxLayout(self.mapping_container)
        map_layout.setContentsMargins(0, 0, 0, 0)
        map_layout.addStretch(1) 
        
        map_layout.addWidget(QLabel("Map Field:"))
        self.cmb_map_field = QComboBox(); self.cmb_map_field.setMinimumWidth(200)
        map_layout.addWidget(self.cmb_map_field)
        
        map_layout.addWidget(QLabel("to Param:"))
        self.cmb_map_param = QComboBox(); self.cmb_map_param.setMinimumWidth(150)
        map_layout.addWidget(self.cmb_map_param)
        
        self.mapping_container.setVisible(False)
        layout.addWidget(self.mapping_container)
        
        # Table buttons
        self.table_button_layout = QHBoxLayout()
        
        # Bulk Amount update
        self.txt_amount = QLineEdit()
        self.txt_amount.setPlaceholderText("Size")
        self.txt_amount.setFixedWidth(60)
        self.btn_update_amount = QPushButton("Set All")
        self.table_button_layout.addWidget(QLabel("Sample Size:"))
        self.table_button_layout.addWidget(self.txt_amount)
        self.table_button_layout.addWidget(self.btn_update_amount)

        # Bulk Container Type update
        self.cmb_container_type_all = QComboBox(); self.cmb_container_type_all.setMinimumWidth(130)
        self.btn_update_container = QPushButton("Set All")
        self.table_button_layout.addWidget(QLabel("  Container:"))
        self.table_button_layout.addWidget(self.cmb_container_type_all)
        self.table_button_layout.addWidget(self.btn_update_container)

        self.table_button_layout.addStretch()
        
        self.btn_add_row = QPushButton("Add Sample Row"); self.btn_delete_row = QPushButton("Delete Selected Row(s)")
        self.table_button_layout.addWidget(self.btn_add_row); self.table_button_layout.addWidget(self.btn_delete_row)
        layout.addLayout(self.table_button_layout)
        
        self.samples_table = QTableView(); self.samples_table.setSelectionBehavior(QTableView.SelectRows)
        self.samples_model = QStandardItemModel()
        
        self.samples_table_headers = [
            "Sample Name", "Sample Type", "Collection Date", "Amount", "Container Type",
            "End Collection Date",
            "Country", "State Code", "Latitude", "Longitude",
            "Elevation", "Length Unit", "Remarks",
            "UserField1", "UserField2", "UserField3", "UserField4", "UserField5"
        ]
        self.samples_model.setHorizontalHeaderLabels(self.samples_table_headers)
        self.samples_table.setModel(self.samples_model)
        self.type_delegate = ComboDelegate(self.samples_table, {})
        self.samples_table.setItemDelegateForColumn(1, self.type_delegate)
        self.container_type_delegate = ComboDelegate(self.samples_table, {})
        self.samples_table.setItemDelegateForColumn(4, self.container_type_delegate)
        self.country_delegate = ComboDelegate(self.samples_table, {})
        self.samples_table.setItemDelegateForColumn(6, self.country_delegate)
        self.samples_table.setColumnHidden(5, True)  # End Collection Date
        layout.addWidget(self.samples_table)
        group.setLayout(layout)
        return group

    def connect_signals(self):
        self.rb_new_submission.toggled.connect(self._on_upload_mode_changed)
        self.rb_append_submission.toggled.connect(self._on_upload_mode_changed)
        for rb in [self.rb_lims_sheet, self.rb_manual_entry, self.rb_json, self.rb_gnip, self.rb_trims]:
            rb.toggled.connect(self._on_upload_source_changed)

        self.save_button.clicked.connect(self.save_submission)
        self.btn_pick_file.clicked.connect(self.pick_lims_submission_sheet)
        self.btn_create_gnip_samples.clicked.connect(self.create_gnip_samples)
        self.btn_reset_gnip.clicked.connect(self.reset_form)
        self.btn_trims_import.clicked.connect(self.import_from_trims)
        self.cmb_available_projects.currentIndexChanged.connect(self.load_project_details)
        self.cmb_media_type.currentIndexChanged.connect(self._on_media_type_changed)
        self.btn_add_row.clicked.connect(self.add_sample_row)
        self.btn_delete_row.clicked.connect(self.delete_sample_row)  
        self.cmb_map_field.currentIndexChanged.connect(self._on_map_field_changed)
        self.cmb_map_param.currentIndexChanged.connect(self._on_map_param_changed)       
        self.rb_unknown_samples.toggled.connect(self._update_sample_type_options)
        self.btn_update_amount.clicked.connect(self.update_all_amounts)
        self.btn_update_container.clicked.connect(self.update_all_container_types)
        
    def _populate_gnip_combos(self):
        current_year = date.today().year + 1
        for i in range(current_year, current_year - 15, -1):
            self.cmb_start_yr.addItem(str(i)); self.cmb_end_yr.addItem(str(i))
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        for i, month in enumerate(months):
            self.cmb_start_mo.addItem(month, i + 1); self.cmb_end_mo.addItem(month, i + 1)
        self.cmb_start_mo.setCurrentIndex(0); self.cmb_end_mo.setCurrentIndex(11)
        
    # --- Logic: Mapping ---
    def _on_map_field_changed(self):
        idx = self.cmb_map_field.currentData()
        if idx is None: return
        self.cmb_map_param.blockSignals(True)
        mapped_id = self.user_field_mappings.get(idx)
        if mapped_id:
            c_idx = self.cmb_map_param.findData(mapped_id)
            self.cmb_map_param.setCurrentIndex(c_idx)
        else: self.cmb_map_param.setCurrentIndex(-1)
        self.cmb_map_param.blockSignals(False)

    def _on_map_param_changed(self):
        col_idx = self.cmb_map_field.currentData()
        meas_id = self.cmb_map_param.currentData()
        meas_name = self.cmb_map_param.currentText()
        if col_idx is None: return

        if meas_id:
            self.user_field_mappings[col_idx] = meas_id
            label = meas_name
        else:
            if col_idx in self.user_field_mappings: del self.user_field_mappings[col_idx]
            label = self.user_field_origins.get(col_idx, f"UserField{col_idx-12}")
            
        self.samples_model.setHeaderData(col_idx, Qt.Horizontal, label)

    def _update_sample_type_options(self):
        is_ref = self.rb_reference_samples.isChecked()
        filtered_map = {}
        for k, v in self.sample_type_map.items():
            if is_ref:
                if k != 0: filtered_map[k] = v
            else:
                if k == 0: filtered_map[k] = v
        self.type_delegate.update_items(filtered_map)

    def update_all_amounts(self):
        val = self.txt_amount.text().strip()
        if not val: return
        try:
            float(val)
        except ValueError:
            QMessageBox.warning(self, "Error", "Sample size must be numeric.")
            return
        for r in range(self.samples_model.rowCount()):
            self.samples_model.item(r, 3).setText(val)

    def update_all_container_types(self):
        ctype_id = self.cmb_container_type_all.currentData()
        ctype_name = self.cmb_container_type_all.currentText()
        if ctype_id is None: return
        for r in range(self.samples_model.rowCount()):
            item = self.samples_model.item(r, 4)
            if item is None:
                item = QStandardItem(); self.samples_model.setItem(r, 4, item)
            item.setText(ctype_name); item.setData(ctype_id, Qt.UserRole)

    # ... Helpers ...
    def _clean_val(self, v):
        if v is None: return None
        s = str(v).strip()
        if not s or s.lower() == "nan" or s.lower() == "none": return None
        return s

    def _sql_concat(self, *args):
        dialect = db_manager.dialect
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')): parts.append(arg)
            else: parts.append(f"CAST({arg} AS NVARCHAR(255))" if dialect == "SQL_SERVER" else str(arg))
        sep = " + " if dialect == "SQL_SERVER" else " || "
        return sep.join(parts)
    def _sql_bool(self, value): return "1" if value else "0"
    def _sql_func(self, func_name, *args):
        func_name = func_name.upper()
        if func_name == "UPPER" and db_manager.dialect == "ACCESS": return f"UCase({args[0]})"
        return f"{func_name}({', '.join(args)})"
    def dlookup(self, field, table, criteria):
        try:
            with db_manager.get_connection() as conn:
                sql = f"SELECT {db_manager.sql_top(1)}{field} FROM {table} WHERE {criteria}{db_manager.sql_limit(1)}"
                result = conn.execute(text(sql)).fetchone()
                return result[0] if result else None
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return None

    def enable_dynamic_search(self, combo):
        """
        Enables 'Contains' filtering for a QComboBox.
        User types 'Add' -> Matches 'Ethiopia--Addis Ababa'
        """
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        
        # Access the completer (automatically created when Editable is True)
        completer = combo.completer()        
        # 1. Show the dropdown list of suggestions
        completer.setCompletionMode(QCompleter.PopupCompletion)        
        # 2. CRITICAL: Match anywhere in the string, not just the start
        completer.setFilterMode(Qt.MatchContains)        
        # 3. Case insensitive (e.g., 'add' matches 'Addis')
        completer.setCaseSensitivity(Qt.CaseInsensitive)        
        # Optional: Ensure the completer uses the same model as the combo
        completer.setModel(combo.model())
        
    def load_initial_data(self):
        logging.info("Loading initial data...")
        try:
            with db_manager.get_connection() as conn:
                try:
                    res = conn.execute(text("SELECT MAX(SubmissionID) FROM Submission")).fetchone()
                    self.txt_submission_id.setText(str((res[0] or 10000) + 1))
                except Exception: self.txt_submission_id.setText("Error")
                
                sql = f"""SELECT MediaID, {db_manager.sql_concat("Prefix", "': '", "medianame")} FROM Media WHERE IsActive = {db_manager.sql_bool(True)} ORDER BY Prefix DESC, MediaID"""
                self.cmb_media_type.clear(); self.cmb_media_type.addItem("", None)
                for row in conn.execute(text(sql)): self.cmb_media_type.addItem(row[1], row[0])
                
                # sql = f"""SELECT lngStationAutoID, {db_manager.sql_concat("Country.sName", "'--'", "tblStation.strStationName")} FROM tblStation INNER JOIN Country ON tblStation.strCountryCode = Country.CountryCode ORDER BY 2"""
                # self.cmb_station.clear(); self.cmb_station.addItem("", None)
                # for row in conn.execute(text(sql)): self.cmb_station.addItem(row[1], row[0])

                # Maps: lngStationAutoID -> StationID, strStationName -> Name, sName -> Name (Country)
                # WHERE Station.StatusID = {db_manager.sql_bool(True)}
                sql = f"""
                    SELECT Station.StationID, 
                        {db_manager.sql_concat("Country.sName", "'--'", "Station.Name")} 
                    FROM Station 
                    INNER JOIN Country ON Station.CountryCode = Country.CountryCode 
                    WHERE Station.StatusID = 12
                    ORDER BY 2
                """
                self.cmb_station.clear(); self.cmb_station.addItem("", None)
                for row in conn.execute(text(sql)): self.cmb_station.addItem(row[1], row[0])

                sql = f"""SELECT CustomerID, {db_manager.sql_concat(self._sql_func("UPPER", "LastName"), "','", "FirstName")} FROM Customer ORDER BY InstitutionName, LastName, FirstName"""
                self.cmb_client.clear(); self.cmb_client.addItem("", None)
                for row in conn.execute(text(sql)): self.cmb_client.addItem(row[1], row[0])
                    
                sql = f"""SELECT EmployeeID, {db_manager.sql_concat(self._sql_func("UPPER", "LastName"), "','", "FirstMiddleName")} FROM Employee ORDER BY LastName, FirstMiddleName"""
                self.cmb_officer.clear(); self.cmb_officer.addItem("", None)
                for row in conn.execute(text(sql)): self.cmb_officer.addItem(row[1], row[0])
                
                sql = f"""SELECT PriorityID, {db_manager.sql_concat("PriorityID", "'--'", "Description")} FROM Priority"""
                self.cmb_priority.clear(); self.cmb_priority.addItem("", None)
                for row in conn.execute(text(sql)): self.cmb_priority.addItem(row[1], row[0])

                self.txt_submission_date.setText(date.today().strftime('%Y-%m-%d'))
                idx = self.cmb_priority.findData(3)
                if idx >= 0: self.cmb_priority.setCurrentIndex(idx)

                self.field_params_list = [("", None)]
                fp_sql = ("SELECT a.AnalyteID, a.ParameterLabel FROM Analytes a "
                          "JOIN Matrix mx ON mx.MatrixID = a.MatrixID "
                          "WHERE mx.MatrixName='Water' AND a.IsFieldParam=1 ORDER BY a.ParameterLabel")
                for row in conn.execute(text(fp_sql)):
                    self.field_params_list.append((row[1], row[0]))
                self.cmb_map_param.clear()
                for name, mid in self.field_params_list:
                    self.cmb_map_param.addItem(name, mid)
                    
                c_sql = "SELECT CountryCode, sName FROM Country ORDER BY sName"
                self.country_map = {None: ""}
                for row in conn.execute(text(c_sql)):
                    self.country_map[row[0]] = row[1]
                self.country_delegate.update_items(self.country_map)

                # Container type lookup (migration 022)
                ct_sql = "SELECT container_type_id, type_name FROM public.container_type_lookup WHERE is_active ORDER BY container_type_id"
                self.container_type_map = {None: ""}
                self.cmb_container_type_all.clear(); self.cmb_container_type_all.addItem("", None)
                for row in conn.execute(text(ct_sql)):
                    self.container_type_map[row[0]] = row[1]
                    self.cmb_container_type_all.addItem(row[1], row[0])
                self.container_type_delegate.update_items(self.container_type_map)
                
                st_sql = "SELECT intSampleType, strShortDescription FROM tblSampleType ORDER BY intSampleType"
                self.sample_type_map = {}
                for row in conn.execute(text(st_sql)):
                    sid, txt = row[0], row[1]
                    if sid == 0: txt = "UNKWN"
                    self.sample_type_map[sid] = txt
                self._update_sample_type_options()

        except Exception as e:
            logging.error(f"Failed to load initial data: {e}", exc_info=True)
            set_status(self.status_label,f"Load failed: {e}", "error")

    def load_available_projects(self):
        is_ref = self.rb_reference_samples.isChecked()
        op = "=" if is_ref else "<>"
        sql = f"""SELECT SubmissionID, {db_manager.sql_concat("SubmissionID", "'---'", "SubmissionName")} FROM Submission WHERE SubmissionType {op} 1 ORDER BY SubmissionDate DESC"""
        try:
            with db_manager.get_connection() as conn:
                self.cmb_available_projects.clear(); self.cmb_available_projects.addItem("", None)
                for row in conn.execute(text(sql)): self.cmb_available_projects.addItem(row[1], row[0])
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
    def load_project_details(self):
        pid = self.cmb_available_projects.currentData()
        if not pid: self.reset_form_fields(set_id=False); return
        try:
            with db_manager.get_connection() as conn:
                sql = "SELECT SubmissionID, SubmissionName, SubmissionSite, SubmissionDate, CustomerID, TechnicalOfficer, MediaID, RequestedWorkflow, PriorityID, PayerID, PayerReference, ReceivingNo, InvoiceID FROM Submission WHERE SubmissionID = :pid"
                row = conn.execute(text(sql), {"pid": pid}).fetchone()
                if row:
                    self.txt_submission_id.setText(str(row[0]))
                    self.txt_submission_name.setText(row[1] or "")
                    self.txt_sampling_site.setText(row[2] or "")
                    self.txt_submission_date.setText(str(row[3].date()) if row[3] else "")
                    self.cmb_client.setCurrentIndex(self.cmb_client.findData(row[4]))
                    self.cmb_officer.setCurrentIndex(self.cmb_officer.findData(row[5]))
                    self.cmb_media_type.setCurrentIndex(self.cmb_media_type.findData(row[6]))
                    self._on_media_type_changed()
                    self.cmb_req_workflow.setCurrentIndex(self.cmb_req_workflow.findData(row[7]))
                    self.cmb_priority.setCurrentIndex(self.cmb_priority.findData(row[8]))
                    self.cmb_payer.setCurrentIndex(self.cmb_payer.findData(row[9]))
                    self.txt_payer_ref.setText(row[10] or "")
                    self.txt_receiving_no.setText(row[11] or "")
                    self.txt_invoice_no.setText(row[12] or "")
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    def upload_samples_from_excel(self, file_path):
        logging.info(f"Importing Excel: {file_path}")
        set_status(self.status_label,"Importing...", "processing")
        QApplication.processEvents()
        try:
            df_head = pd.read_excel(file_path, sheet_name="Headings", header=None)
            def get_meta(keys):
                for k in keys:
                    match = df_head[df_head.iloc[:, 0] == k]
                    if not match.empty: return str(match.iloc[0, 1])
                return ""
            sub_date = get_meta(["Submission Date*:", "Date:"])
            if sub_date: self.txt_submission_date.setText(sub_date)
            self.txt_submission_name.setText(get_meta(["Project Name*:", "Project Name:"]))
            self.txt_sampling_site.setText(get_meta(["General Location*:", "General Location:"]))
            last = get_meta(["Last Name*:", "Last Name:"]); first = get_meta(["First Name*:", "First Name:"])
            if last:
                cid = self.dlookup("CustomerID", "Customer", f"LastName='{last}' AND FirstName='{first}'")
                if cid: self.cmb_client.setCurrentIndex(self.cmb_client.findData(cid))
            m_str = get_meta(["Media Code*:", "Media Code:"])
            if m_str:
                try:
                    mid = int(m_str.split('(')[-1].replace(')', ''))
                    self.cmb_media_type.setCurrentIndex(self.cmb_media_type.findData(mid))
                    self._on_media_type_changed()
                except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

            header_idx = -1
            for i, row in df_head.iterrows():
                if "Sample ID" in row.values: header_idx = i; break
            if header_idx == -1: set_status(self.status_label,"Error: 'Sample ID' not found.", "error"); return

            main_headers = df_head.iloc[header_idx].fillna("").astype(str).tolist()
            data_start = -1
            for i in range(header_idx + 2, len(df_head)):
                try: float(df_head.iloc[i, 0]); data_start = i; break
                except: continue
            if data_start == -1: set_status(self.status_label,"Error: No data rows found.", "error"); return

            data_df = df_head.iloc[data_start:].copy()
            data_df.columns = main_headers
            
            col_map = { "Sample Name": "Sample ID", "Collection Date": "Collection Date/Time", "Country": "Country Code", "State Code": "State/Province Code", "Latitude": "Degrees Latitude", "Longitude": "Degrees Longitude", "Elevation": "Elevation", "Length Unit": "Length Unit", "Remarks": "Other Info" }
            
            standard_cols = list(col_map.values())
            ignore_list = ["Counter", "Ending Date/Time", "Ending Date"]
            extra_headers = [h for h in main_headers if h not in standard_cols and h not in ignore_list and h.strip()]
            
            self.cmb_map_field.clear()
            self.user_field_mappings = {}
            self.user_field_origins = {}
            count = 0
            for h in extra_headers:
                if count >= 5: break
                col_idx = 13 + count   # shifted by 1 (Container Type inserted at 4)
                self.user_field_origins[col_idx] = h
                self.cmb_map_field.addItem(f"User Field {count+1}: {h}", col_idx)
                col_map[f"UserField{count+1}"] = h

                idx = self.cmb_map_param.findText(h, Qt.MatchContains)
                if idx >= 0:
                    meas_id = self.cmb_map_param.itemData(idx)
                    self.user_field_mappings[col_idx] = meas_id
                    self.samples_model.setHeaderData(col_idx, Qt.Horizontal, self.cmb_map_param.itemText(idx))
                else:
                    self.samples_model.setHeaderData(col_idx, Qt.Horizontal, h)
                count += 1
            
            self.mapping_container.setVisible(count > 0)

            for _, row in data_df.iterrows():
                s_name_raw = row.get("Sample ID", "")
                s_name = self._clean_val(s_name_raw)
                if not s_name: continue
                
                model_row = []
                for h in self.samples_table_headers:
                    if h == "Sample Type":
                        item = QStandardItem("UNKWN")
                        item.setData(0, Qt.UserRole)
                        item.setData("UNKWN", Qt.DisplayRole)
                        model_row.append(item)
                    elif h == "Country":
                        code = self._clean_val(row.get(col_map.get(h), ""))
                        name = self.country_map.get(code, code) if code else ""
                        item = QStandardItem(name)
                        item.setData(code, Qt.UserRole) 
                        model_row.append(item)
                    elif h == "Collection Date":
                        val = self._clean_val(row.get(col_map.get(h), ""))
                        if val:
                            try:
                                dt_val = pd.to_datetime(val, errors='coerce')
                                val = dt_val.strftime('%Y-%m-%d') if not pd.isna(dt_val) else val
                            except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
                        model_row.append(QStandardItem(val or ""))
                    elif h == "Amount":
                        model_row.append(QStandardItem(""))
                    elif h == "Container Type":
                        item = QStandardItem(""); item.setData(None, Qt.UserRole); model_row.append(item)
                    elif h in col_map:
                        val = self._clean_val(row.get(col_map[h], ""))
                        model_row.append(QStandardItem(val or ""))
                    else:
                        model_row.append(QStandardItem(""))
                self.samples_model.appendRow(model_row)
            
            self.samples_table.resizeColumnsToContents()
            set_status(self.status_label,f"Success: Imported {self.samples_model.rowCount()} samples.", "success")

        except Exception as e:
            logging.error(f"Excel Import Error: {e}")
            set_status(self.status_label,f"Import Error: {e}", "error")

    def upload_samples_from_json(self, file_path): pass
    
    def import_from_trims(self):
        trims_id_str = self.txt_trims_id.text().strip()
        if not trims_id_str:
            QMessageBox.warning(self, "Input Error", "Please enter a TRIMS ID.")
            return

        try:
            trims_id = int(trims_id_str)
        except ValueError:
            QMessageBox.warning(self, "Input Error", "TRIMS ID must be a number.")
            return

        trims_db_path = self.get_config_text("TRIMSDBPath")
        if not trims_db_path or not os.path.exists(trims_db_path):
             QMessageBox.warning(self, "Config Error", f"TRIMS DB path not found in config.\nPath: {trims_db_path}")
             return

        # --- DRIVER DETECTION LOGIC ---
        drivers = [d for d in pyodbc.drivers() if "Access" in d and "(*.mdb, *.accdb)" in d]
        if not drivers:
            # Fallback: Check for older mdb driver if valid, though unlikely for accdb
            drivers = [d for d in pyodbc.drivers() if "Access" in d]
            
        if not drivers:
            QMessageBox.critical(self, "Driver Error", 
                "No Microsoft Access ODBC drivers found.\n\n"
                "Please install the 'Microsoft Access Database Engine 2016 Redistributable' (match your Python bit-version: 64-bit usually).")
            return
            
        # Use the first found valid driver
        driver_name = drivers[0]
        # ------------------------------

        trims_conn = None
        try:
            # Use detected driver
            conn_str = f"DRIVER={{{driver_name}}};DBQ={trims_db_path};"
            trims_conn = pyodbc.connect(conn_str)
            trims_cursor = trims_conn.cursor()
            
            # ... (Rest of the function remains identical) ...
            import_type = self.cmb_trims_option.currentIndex() 

            if import_type == 1: # LSC RUN
                sql = """
                    SELECT tblProject.lngProjectID, tblProject.strProjectName, strProjectLocation, dtmSubmissionDate, tblWorkflow.strWorkflowAbbreviation,
                    lngSampleVolume, strCountry, tblCustomer.strLastName AS Submitter, tblEmployee.strLastName AS Manager,
                    lngRequestedWorkflow, tblSample.dblECField, tblSample.lngSampleID, tblSample.strFieldName, tblSample.intStatus
                    FROM (((((tblProject INNER JOIN tblSample ON tblSample.lngProjectID = tblProject.lngProjectID)
                    INNER JOIN tblSampleAnalysis ON tblSampleAnalysis.lngSampleID = tblSample.lngSampleID)
                    INNER JOIN tblCounterLoadList ON tblCounterLoadList.lngAnalysisID=tblSampleAnalysis.lngAnalysisID)
                    INNER JOIN tblWorkflow ON tblWorkflow.intWorkflowID = tblSampleAnalysis.intWorkflowID)
                    LEFT JOIN tblCustomer ON tblCustomer.lngCustomer = tblProject.lngCustomerID)
                    LEFT JOIN tblEmployee ON tblProject.lngTechnicalOfficerID = tblEmployee.lngEmployeeID
                    WHERE tblProject.lngProjectID<>1 AND tblCounterLoadList.lngCounterRunID = ?
                    ORDER BY tblCounterLoadList.lngAnalysisID
                """
            else: # Project #
                sql = """
                    SELECT tblProject.lngProjectID, tblProject.strProjectName, strProjectLocation, dtmSubmissionDate, tblWorkflow.strWorkflowAbbreviation,
                    lngSampleVolume, strCountry, tblCustomer.strLastName AS Submitter, tblEmployee.strLastName AS Manager,
                    lngRequestedWorkflow, tblSample.dblECField, tblSample.lngSampleID, tblSample.strFieldName, tblSample.intStatus
                    FROM (((tblProject INNER JOIN tblSample ON tblSample.lngProjectID = tblProject.lngProjectID)
                    LEFT JOIN tblCustomer ON tblCustomer.lngCustomer = tblProject.lngCustomerID)
                    LEFT JOIN tblEmployee ON tblProject.lngTechnicalOfficerID = tblEmployee.lngEmployeeID)
                    INNER JOIN tblWorkflow ON tblWorkflow.intWorkflowID = tblProject.lngRequestedWorkflow
                    WHERE tblProject.lngProjectID<>1 AND tblProject.lngProjectID = ?
                    ORDER BY tblSample.lngSampleID
                """

            trims_cursor.execute(sql, (trims_id,))
            rows = trims_cursor.fetchall()

            if not rows:
                QMessageBox.information(self, "No Data", f"No records found for ID {trims_id} in TRIMS.")
                return

            first_row = rows[0]
            proj_name = first_row[1]
            proj_loc = first_row[2]
            sub_date = first_row[3]
            wf_abbr = first_row[4]
            submitter_name = first_row[7]
            manager_name = first_row[8]

            if import_type == 1:
                self.txt_sampling_site.setText(f"Imported TRIMS LSC RUN {trims_id}")
                self.txt_submission_name.setText(f"Imported TRIMS LSC RUN {trims_id}")
            else:
                self.txt_sampling_site.setText(proj_loc or "")
                self.txt_submission_name.setText(proj_name or "")

            if sub_date:
                self.txt_submission_date.setText(sub_date.strftime('%Y-%m-%d'))

            idx_pri = self.cmb_priority.findData(3)
            if idx_pri >= 0: self.cmb_priority.setCurrentIndex(idx_pri)

            idx_media = self.cmb_media_type.findData(200)
            if idx_media >= 0: 
                self.cmb_media_type.setCurrentIndex(idx_media)
            else:
                self.cmb_media_type.setCurrentIndex(1)
            self._on_media_type_changed()

            if manager_name:
                mgr_search = "Terzer-Wassmuth" if manager_name == "Terzer" else (manager_name or "WANGARI")
                idx_mgr = self.cmb_officer.findText(mgr_search, Qt.MatchContains)
                if idx_mgr >= 0: self.cmb_officer.setCurrentIndex(idx_mgr)

            if submitter_name:
                sub_search = "Terzer-Wassmuth" if submitter_name == "Terzer" else submitter_name
                idx_sub = self.cmb_client.findText(sub_search, Qt.MatchContains)
                if idx_sub >= 0: self.cmb_client.setCurrentIndex(idx_sub)

            if wf_abbr:
                idx_wf = self.cmb_req_workflow.findText(wf_abbr, Qt.MatchContains)
                if idx_wf >= 0: self.cmb_req_workflow.setCurrentIndex(idx_wf)

            self.samples_model.clear()
            self.samples_model.setHorizontalHeaderLabels(self.samples_table_headers)

            for row in rows:
                s_name = row[12]
                c_date = row[3].strftime('%Y-%m-%d') if row[3] else ""
                vol = row[5]
                cntry = row[6]
                
                model_row = []
                for h in self.samples_table_headers:
                    if h == "Sample Name": model_row.append(QStandardItem(str(s_name or "")))
                    elif h == "Sample Type":
                        item = QStandardItem("UNKWN"); item.setData(0, Qt.UserRole); item.setData("UNKWN", Qt.DisplayRole); model_row.append(item)
                    elif h == "Collection Date": model_row.append(QStandardItem(c_date))
                    elif h == "Amount": model_row.append(QStandardItem(str(vol) if vol else ""))
                    elif h == "Container Type":
                        item = QStandardItem(""); item.setData(None, Qt.UserRole); model_row.append(item)
                    elif h == "Country":
                        code = None
                        for c, n in self.country_map.items():
                            if n and cntry and n.lower() == cntry.lower():
                                code = c; break
                        item = QStandardItem(cntry or "")
                        if code: item.setData(code, Qt.UserRole)
                        model_row.append(item)
                    else: model_row.append(QStandardItem(""))
                
                self.samples_model.appendRow(model_row)

            self.samples_table.resizeColumnsToContents()
            set_status(self.status_label,f"Imported {len(rows)} samples from TRIMS.", "success")

        except Exception as e:
            logging.error(f"TRIMS Processing Error: {e}", exc_info=True)
            QMessageBox.critical(self, "Import Error", f"Error processing TRIMS data:\n{e}")
        finally:
            if trims_conn: trims_conn.close()

    # def create_gnip_samples(self):
        
    #     try:
    #         with db_manager.get_connection() as conn:
    #             station_id = self.cmb_station.currentData()
    #             print(f"stating to load GNIP ... {station_id}")
    #             if not station_id: return
    #             sql_st = "SELECT strStationLimsName, strStationName, strCountryCode, intTrimsMainWorkflow, dblLatitude, dblLongitude FROM tblStation WHERE lngStationAutoID = :sid"
    #             st = conn.execute(text(sql_st), {"sid": station_id}).fetchone()
    #             if not st: return
    #             station_name = st0 or st1; country = st2; lat = st4; lon = st5
                
    #             media_id = self.cmb_media_type.currentData()
    #             if not media_id: return
    #             sql_wf = f"SELECT WorkflowID FROM Workflow WHERE IsObsolete = {db_manager.sql_bool(False)} AND MediaID = :mid"
    #             wf = conn.execute(text(sql_wf), {"mid": media_id}).fetchone()
    #             if wf: self.cmb_req_workflow.setCurrentIndex(self.cmb_req_workflow.findData(wf0))

    #             try:
    #                 y1, y2 = int(self.cmb_start_yr.currentText()), int(self.cmb_end_yr.currentText())
    #                 m1, m2 = self.cmb_start_mo.currentData(), self.cmb_end_mo.currentData()
    #             except: return
                
    #             max_dilution = self.get_config_val("maxAllowableDilutionFactor", -1)
    #             min_vol = -999.0
                
    #             self.samples_model.clear()
    #             self.samples_model.setHorizontalHeaderLabels(self.samples_table_headers)
                
    #             for year in range(y1, y2 + 1):
    #                 start_m = m1 if year == y1 else 1
    #                 end_m = m2 if year == y2 else 12
    #                 for month in range(start_m, end_m + 1):
    #                     name = f"{station_name} {year}{month:02d}15"; c_date = f"{year}-{month:02d}-15"
    #                     row = []
    #                     for h in self.samples_table_headers:
    #                         if h == "Sample Name": row.append(QStandardItem(name))
    #                         elif h == "Sample Type": 
    #                             item = QStandardItem("UNKWN"); item.setData(0, Qt.UserRole); item.setData("UNKWN", Qt.DisplayRole); row.append(item)
    #                         elif h == "Collection Date": row.append(QStandardItem(c_date))
    #                         elif h == "Country": 
    #                             c_name = self.country_map.get(country, country)
    #                             item = QStandardItem(c_name); item.setData(country, Qt.UserRole); row.append(item)
    #                         elif h == "Latitude": row.append(QStandardItem(str(lat or "")))
    #                         elif h == "Longitude": row.append(QStandardItem(str(lon or "")))
    #                         else: row.append(QStandardItem(""))
    #                     self.samples_model.appendRow(row)
                
    #             self.txt_sampling_site.setText(self.cmb_station.currentText())
    #             self.txt_submission_name.setText(f"GNIP_{station_name}_{y2}")
    #             self.samples_table.resizeColumnsToContents()
    #             set_status(self.status_label,f"Generated {self.samples_model.rowCount()} GNIP samples.", "success")

    #     except Exception as e:
    #         logging.error(f"GNIP Error: {e}")
    #         set_status(self.status_label,f"GNIP Error: {e}", "error")

    def create_gnip_samples(self):
            try:
                with db_manager.get_connection() as conn:
                    station_id = self.cmb_station.currentData()
                    if not station_id: 
                        return

                    sql_st = """
                        SELECT ShortName, Name, CountryCode, Latitude, Longitude 
                        FROM Station 
                        WHERE StationID = :sid
                    """
                    st = conn.execute(text(sql_st), {"sid": station_id}).fetchone()
                    if not st: 
                        return
                    
                    # Logic: Use ShortName (LIMS name) if available, otherwise full Name
                    station_name = st[0] if st[0] else st[1] 
                    country = st[2]
                    lat = st[3]
                    lon = st[4]
                    
                    # --- CHANGE 2: Verify Workflow Query (Standardized) ---
                    media_id = self.cmb_media_type.currentData()
                    if not media_id: 
                        return
                    
                    # This query is mostly compatible, just ensuring column casing
                    sql_wf = f"SELECT WorkflowID FROM Workflow WHERE IsObsolete = {db_manager.sql_bool(False)} AND MediaID = :mid"
                    wf = conn.execute(text(sql_wf), {"mid": media_id}).fetchone()
                    if wf: 
                        self.cmb_req_workflow.setCurrentIndex(self.cmb_req_workflow.findData(wf[0]))

                    # --- Date Parsing (No changes needed here) ---
                    try:
                        y1, y2 = int(self.cmb_start_yr.currentText()), int(self.cmb_end_yr.currentText())
                        m1, m2 = self.cmb_start_mo.currentData(), self.cmb_end_mo.currentData()
                    except: 
                        return
                    
                    self.samples_model.clear()
                    self.samples_model.setHorizontalHeaderLabels(self.samples_table_headers)
                    
                    # --- Sample Generation Loop ---
                    for year in range(y1, y2 + 1):
                        start_m = m1 if year == y1 else 1
                        end_m = m2 if year == y2 else 12
                        for month in range(start_m, end_m + 1):
                            # Construct Sample Name: "StationName YYYYMM15"
                            name = f"{station_name} {year}{month:02d}15"
                            c_date = f"{year}-{month:02d}-15"
                            
                            row = []
                            for h in self.samples_table_headers:
                                if h == "Sample Name": 
                                    row.append(QStandardItem(name))
                                elif h == "Sample Type": 
                                    item = QStandardItem("UNKWN")
                                    item.setData(0, Qt.UserRole)
                                    item.setData("UNKWN", Qt.DisplayRole)
                                    row.append(item)
                                elif h == "Collection Date":
                                    row.append(QStandardItem(c_date))
                                elif h == "Container Type":
                                    item = QStandardItem(""); item.setData(None, Qt.UserRole); row.append(item)
                                elif h == "Country":
                                    # Resolve Country Name from Code
                                    c_name = self.country_map.get(country, country)
                                    item = QStandardItem(c_name)
                                    item.setData(country, Qt.UserRole)
                                    row.append(item)
                                elif h == "Latitude": 
                                    row.append(QStandardItem(str(lat) if lat is not None else ""))
                                elif h == "Longitude": 
                                    row.append(QStandardItem(str(lon) if lon is not None else ""))
                                else: 
                                    row.append(QStandardItem(""))
                            self.samples_model.appendRow(row)
                    
                    self.txt_sampling_site.setText(self.cmb_station.currentText())
                    self.txt_submission_name.setText(f"GNIP_{station_name}_{y2}")
                    self.samples_table.resizeColumnsToContents()
                    set_status(self.status_label, f"Generated {self.samples_model.rowCount()} GNIP samples.", "success")

            except Exception as e:
                logging.error(f"GNIP Error: {e}")
                set_status(self.status_label, f"GNIP Error: {e}", "error")
            
    def reset_form_fields(self, set_id=False):
        self.txt_submission_name.clear(); self.txt_sampling_site.clear()
        self.mapping_container.setVisible(False)
        self.user_field_mappings = {}
        self.user_field_origins = {}
        self.cmb_map_field.clear()
        if set_id:
            try:
                with db_manager.get_connection() as conn:
                    res = conn.execute(text("SELECT MAX(SubmissionID) FROM Submission")).fetchone()
                    self.txt_submission_id.setText(str((res[0] or 10000) + 1))
            except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
    def reset_form(self): self.reset_form_fields(set_id=True); self.samples_model.clear(); set_status(self.status_label,"Reset.", "neutral")
    def _on_media_type_changed(self):
        mid = self.cmb_media_type.currentData()
        if not mid: self.cmb_req_workflow.clear(); return
        try:
            with db_manager.get_connection() as conn:
                sql = f"""SELECT WorkflowID, {db_manager.sql_concat("WorkflowID", "'--'", "WorkflowName")} FROM Workflow WHERE IsObsolete = {db_manager.sql_bool(False)} AND MediaID = :mid ORDER BY WorkflowID"""
                self.cmb_req_workflow.clear(); self.cmb_req_workflow.addItem("", None)
                for row in conn.execute(text(sql), {"mid": mid}): self.cmb_req_workflow.addItem(row[1], row[0])
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
    def _on_station_changed(self): pass
    def _on_upload_mode_changed(self):
        if not self.rb_append_submission.isChecked():
            self.cmb_available_projects.setEnabled(False); self.cmb_available_projects.clear()
            self.cmb_media_type.setEnabled(True); self.cmb_req_workflow.setEnabled(True)
            self.reset_form_fields(set_id=True)
        else:
            self.cmb_available_projects.setEnabled(True)
            self.cmb_media_type.setEnabled(False); self.cmb_req_workflow.setEnabled(False)
            self.load_available_projects()
    def _on_upload_source_changed(self):
        if not self.isEnabled(): return 
        is_lims_or_json = self.rb_lims_sheet.isChecked() or self.rb_json.isChecked()
        is_gnip = self.rb_gnip.isChecked(); is_trims = self.rb_trims.isChecked()
        is_manual = self.rb_manual_entry.isChecked()
        self.file_upload_group.setVisible(is_lims_or_json); self.gnip_group.setVisible(is_gnip); self.trims_group.setVisible(is_trims)
        self.btn_add_row.setVisible(is_manual); self.btn_delete_row.setVisible(is_manual)
    def pick_lims_submission_sheet(self):
        file_name, _ = QFileDialog.getOpenFileName(self, "Import Submission", "", "Excel Files (*.xls *.xlsx);;JSON Files (*.json *.xml)")
        if file_name:
            self.txt_upload_filename.setText(file_name)
            if self.rb_lims_sheet.isChecked(): self.upload_samples_from_excel(file_name)
    
    def add_sample_row(self):
        row = [QStandardItem("") for _ in range(18)]
        row[1].setData("UNKWN", Qt.DisplayRole); row[1].setData(0, Qt.UserRole)
        row[4].setData(None, Qt.UserRole)  # Container Type
        self.samples_model.appendRow(row)
    def delete_sample_row(self): 
        for idx in sorted(set(i.row() for i in self.samples_table.selectionModel().selectedRows()), reverse=True): self.samples_model.removeRow(idx)
    def get_project_id_logic(self): return -1, True, None

    def stage_samples_to_tba(self, submission_id):
        """
        Copies newly created samples from this Submission into sample_queue.
        """
        logging.info(f"Staging samples for Submission {submission_id}...")

        wf_id = self.cmb_req_workflow.currentData()
        pri_id = self.cmb_priority.currentData()

        if not wf_id:
            logging.warning("Cannot stage: No workflow selected.")
            return

        try:
            with db_manager.get_connection() as conn:
                # 1. Get Workflow Job (RunSequence 1)
                res = conn.execute(text("SELECT WorkflowJobID FROM WorkflowJob WHERE WorkflowID=:w AND RunSequence=1"), {"w": wf_id}).fetchone()
                if not res:
                    logging.error("No starting job found for workflow.")
                    return
                wf_job_id = res[0]

                now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                user_str = "PythonUser"

                # 2. Insert into sample_queue (deduped by unique constraint)
                sql_ins = """
                    INSERT INTO public.sample_queue
                        (sampleid, prefix, mediaid, workflowjobid, priorityid,
                         repeat_count, queued_at, queued_by)
                    SELECT sampleid, prefix, mediaid, :job, :pri, 1, :now, :usr
                    FROM public.sample WHERE submissionid=:sid AND sampletype=0
                    ON CONFLICT (sampleid, prefix, workflowjobid) DO NOTHING
                """

                conn.execute(text(sql_ins), {
                    "job": wf_job_id, "pri": pri_id,
                    "now": now_str, "usr": user_str, "sid": submission_id
                })
                
                # 3. Update Status
                conn.execute(text("UPDATE Sample SET Status=222 WHERE SubmissionID=:sid AND SampleType=0"), {"sid": submission_id})
                conn.execute(text("UPDATE Submission SET Status=200 WHERE SubmissionID=:sid"), {"sid": submission_id})
                
                conn.commit()
                logging.info(f"Staging complete for Submission {submission_id}")
                
        except Exception as e:
            logging.error(f"Staging failed: {e}")
            QMessageBox.warning(self, "Staging Error", f"Samples saved, but failed to stage to TBA:\n{e}")
            
    def save_submission(self):
        project_id, is_new, err = self.get_project_id_logic()
        if err: 
            QMessageBox.warning(self, "Error", err); return
        if self.samples_model.rowCount() == 0:
            QMessageBox.warning(self, "Error", "No samples to save."); return

        engine = db_manager.get_engine()
        try:
            with engine.begin() as conn:
                # 1. Submission
                if is_new:
                    final_pid = conn.execute(text("""
                        INSERT INTO Submission
                            (CustomerID, TechnicalOfficer, PayerID, MediaID, SubmissionName,
                             SubmissionSite, SubmissionDate, PriorityID, RequestedWorkflow,
                             Remarks, CreateDateStamp, CreateUserStamp, SubmissionType, Status)
                        VALUES
                            (:cid, :tof, :pay, :med, :nam,
                             :sit, :dat, :pri, :req,
                             :rem, :now, :usr, :typ, 1)
                        RETURNING SubmissionID
                    """), {
                        "cid": self.cmb_client.currentData(), "tof": self.cmb_officer.currentData(),
                        "pay": self.cmb_payer.currentData(), "med": self.cmb_media_type.currentData(),
                        "nam": self.txt_submission_name.text(), "sit": self.txt_sampling_site.text(),
                        "dat": self.txt_submission_date.text(), "pri": self.cmb_priority.currentData(),
                        "req": self.cmb_req_workflow.currentData(), "rem": "Imported from PyLIMS",
                        "now": datetime.now(), "usr": "PythonUser", "typ": 1 if self.rb_reference_samples.isChecked() else 2
                    }).scalar()
                else: final_pid = project_id
                
                # 2. Samples
                prefix = self.dlookup("Prefix", "Media", f"MediaID={self.cmb_media_type.currentData()}")
                max_sid_res = conn.execute(text(f"SELECT MAX(SampleID) FROM Sample WHERE SampleType=0 AND Prefix='{prefix}'")).fetchone()
                curr_sid = (max_sid_res[0] or 0) + 1
                
                sql_samp = """INSERT INTO Sample (SubmissionID, Prefix, SampleID, sName, CollectionDate, CountryCode, Remarks, MediaID, WorkflowID, Status, SampleType, SampleVolume, container_type, CreateUserStamp, CreateDateStamp) VALUES (:sub, :pfx, :sid, :snm, :col, :cnt, :rem, :mid, :wid, 1, :typ, :siz, :ctype, :usr, :now)"""
                sql_field = """INSERT INTO Sample_FieldData (Prefix, SampleID, MeasurableID, FieldValue) VALUES (:pfx, :sid, :mid, :val)"""

                for r in range(self.samples_model.rowCount()):
                    s_name = self.samples_model.item(r, 0).text()
                    idx_t = self.samples_model.index(r, 1)
                    s_type = int(self.samples_model.data(idx_t, Qt.UserRole) or 0)

                    c_date = self.samples_model.item(r, 2).text()

                    amt_txt = self.samples_model.item(r, 3).text().strip()
                    try: size = float(amt_txt) if amt_txt else None
                    except: size = None

                    # Container Type at index 4 (UserRole = ID)
                    idx_ct = self.samples_model.index(r, 4)
                    ctype = self.samples_model.data(idx_ct, Qt.UserRole)

                    # Country from UserRole (index 6 after Container Type insertion)
                    idx_c = self.samples_model.index(r, 6)
                    cntry = self.samples_model.data(idx_c, Qt.UserRole)
                    if not cntry: cntry = self.samples_model.data(idx_c, Qt.DisplayRole)
                    cntry = self._clean_val(cntry)

                    rem = self.samples_model.item(r, 12).text()  # Remarks at index 12

                    this_sid = 0
                    if s_type == 0:
                        this_sid = curr_sid; curr_sid += 1

                    conn.execute(text(sql_samp), {
                        "sub": final_pid, "pfx": prefix, "sid": this_sid, "snm": s_name,
                        "col": c_date if c_date else None, "cnt": cntry, "rem": rem,
                        "mid": self.cmb_media_type.currentData(), "wid": self.cmb_req_workflow.currentData(),
                        "typ": s_type, "siz": size, "ctype": ctype,
                        "usr": "PythonUser", "now": datetime.now()
                    })

                    # 3. Fields
                    if s_type == 0:
                        for col_idx, meas_id in self.user_field_mappings.items():
                            val_text = self.samples_model.item(r, col_idx).text().strip()
                            if val_text:
                                try:
                                    val_dbl = float(val_text)
                                    conn.execute(text(sql_field), {
                                        "pfx": prefix, "sid": this_sid,
                                        "mid": meas_id, "val": val_dbl
                                    })
                                except ValueError: pass

            set_status(self.status_label,f"Success: Saved {final_pid}.", "success")
            QMessageBox.information(self, "Success", f"Saved Submission {final_pid}.")
            self.reset_form()

        except Exception as e:
            logging.error(f"Save failed: {e}")
            set_status(self.status_label,f"Save failed: {e}", "error")
            QMessageBox.critical(self, "Error", f"Transaction failed:\n{e}")

def make_sample_submission_widget():
    return SampleSubmissionWindow()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    win = SampleSubmissionWindow()
    win.show()
    sys.exit(app.exec_())