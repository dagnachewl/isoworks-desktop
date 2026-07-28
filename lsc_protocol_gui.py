"""
LSC Protocol Management GUI
Dialog for creating, editing, and managing LSC analysis protocols
"""
from __future__ import annotations

import logging
from typing import Optional
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QPushButton, QTableWidget, QTableWidgetItem, QLabel, QLineEdit,
    QComboBox, QTextEdit, QDoubleSpinBox, QSpinBox, QCheckBox,
    QMessageBox, QListWidget, QListWidgetItem, QSplitter, QTabWidget,
    QHeaderView, QWidget
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont

from lsc_protocol_manager import (
    LSCProtocol, LSCProtocolManager, ProtocolSettings, ColumnMapping
)
from db_core import db_manager
from sqlalchemy import text


class ProtocolEditorDialog(QDialog):
    """Dialog for editing a single protocol"""
    
    def __init__(self, parent=None, protocol: Optional[LSCProtocol] = None,
                 isotope_id: int = 200, file_format_id: int = 6,
                 available_headers: Optional[list] = None):
        super().__init__(parent)
        self.protocol = protocol or LSCProtocol(isotope_id=isotope_id, file_format_id=file_format_id)
        self.isotope_id = isotope_id
        self.file_format_id = file_format_id
        self.available_headers = available_headers or []  # Simple: just pass the list!
        
        self.setWindowTitle('Protocol Editor')
        self.setMinimumSize(900, 700)
        self._init_ui()
        self._load_protocol_to_ui()
    
    def _init_ui(self):
        layout = QVBoxLayout(self)
        
        # Header info
        header_group = QGroupBox("Protocol Information")
        header_layout = QGridLayout()
        
        header_layout.addWidget(QLabel("Protocol Name:"), 0, 0)
        self.txtName = QLineEdit()
        header_layout.addWidget(self.txtName, 0, 1, 1, 3)
        
        header_layout.addWidget(QLabel("Description:"), 1, 0)
        self.txtDesc = QTextEdit()
        self.txtDesc.setMaximumHeight(60)
        header_layout.addWidget(self.txtDesc, 1, 1, 1, 3)
        
        self.chkDefault = QCheckBox("Set as default for this isotope-format")
        header_layout.addWidget(self.chkDefault, 2, 0, 1, 2)
        
        self.chkActive = QCheckBox("Active")
        self.chkActive.setChecked(True)
        header_layout.addWidget(self.chkActive, 2, 2, 1, 2)
        
        header_group.setLayout(header_layout)
        layout.addWidget(header_group)
        
        # Tabs for Mappings and Settings
        tabs = QTabWidget()
        
        # Tab 1: Column Mappings
        mapping_tab = QWidget()
        mapping_layout = QVBoxLayout(mapping_tab)
        
        mapping_layout.addWidget(QLabel("Column Mappings (File → TRIMS)"))
        
        self.tblMappings = QTableWidget()
        self.tblMappings.setColumnCount(6)
        self.tblMappings.setHorizontalHeaderLabels([
            'Target Field', 'Source Header', 'Is Net?', 'Uncertainty Col', 'Order', 'Notes'
        ])
        self.tblMappings.horizontalHeader().setStretchLastSection(True)
        mapping_layout.addWidget(self.tblMappings)
        
        btn_layout = QHBoxLayout()
        self.btnAddMapping = QPushButton("Add Mapping")
        self.btnAddMapping.clicked.connect(self._add_mapping_row)
        self.btnDeleteMapping = QPushButton("Delete Selected")
        self.btnDeleteMapping.clicked.connect(self._delete_mapping_row)
        btn_layout.addWidget(self.btnAddMapping)
        btn_layout.addWidget(self.btnDeleteMapping)
        btn_layout.addStretch()
        mapping_layout.addLayout(btn_layout)
        
        tabs.addTab(mapping_tab, "Column Mappings")
        
        # Tab 2: Calculation Settings
        settings_tab = QWidget()
        settings_layout = QGridLayout(settings_tab)
        
        row = 0
        
        # Signal Processing Group
        settings_layout.addWidget(QLabel("<b>Signal Processing</b>"), row, 0, 1, 4)
        row += 1
        
        settings_layout.addWidget(QLabel("Signal Metric:"), row, 0)
        self.cmbSignalMetric = QComboBox()
        self.cmbSignalMetric.addItems(['CPM', 'DPM'])
        settings_layout.addWidget(self.cmbSignalMetric, row, 1)
        
        settings_layout.addWidget(QLabel("Efficiency Source:"), row, 2)
        self.cmbEffSource = QComboBox()
        self.cmbEffSource.addItems([
            'Per-cycle (file)', 'Per-run (computed)', 'Per-run (file)'
        ])
        settings_layout.addWidget(self.cmbEffSource, row, 3)
        row += 1
        
        # Background Handling
        settings_layout.addWidget(QLabel("<b>Background Handling</b>"), row, 0, 1, 4)
        row += 1
        
        settings_layout.addWidget(QLabel("Background Mode:"), row, 0)
        self.cmbBkgMode = QComboBox()
        self.cmbBkgMode.addItems(['UseFile', 'Compute', 'Manual'])
        self.cmbBkgMode.currentTextChanged.connect(self._on_bkg_mode_changed)
        settings_layout.addWidget(self.cmbBkgMode, row, 1)
        
        settings_layout.addWidget(QLabel("Manual Value:"), row, 2)
        self.spnBkgValue = QDoubleSpinBox()
        self.spnBkgValue.setRange(0, 10000)
        self.spnBkgValue.setDecimals(3)
        self.spnBkgValue.setEnabled(False)
        settings_layout.addWidget(self.spnBkgValue, row, 3)
        row += 1
        
        # Outlier Detection
        settings_layout.addWidget(QLabel("<b>Outlier Detection</b>"), row, 0, 1, 4)
        row += 1
        
        settings_layout.addWidget(QLabel("Method:"), row, 0)
        self.cmbOutlierMethod = QComboBox()
        self.cmbOutlierMethod.addItems([
            'None', 'Chauvenet', '2σ', '3σ', 'Grubbs', 'Dixon'
        ])
        settings_layout.addWidget(self.cmbOutlierMethod, row, 1)
        
        settings_layout.addWidget(QLabel("Threshold:"), row, 2)
        self.spnOutlierThreshold = QDoubleSpinBox()
        self.spnOutlierThreshold.setRange(1.0, 5.0)
        self.spnOutlierThreshold.setValue(2.0)
        self.spnOutlierThreshold.setSingleStep(0.1)
        settings_layout.addWidget(self.spnOutlierThreshold, row, 3)
        row += 1
        
        settings_layout.addWidget(QLabel("Iterations:"), row, 0)
        self.spnOutlierIter = QSpinBox()
        self.spnOutlierIter.setRange(1, 10)
        self.spnOutlierIter.setValue(3)
        settings_layout.addWidget(self.spnOutlierIter, row, 1)
        
        settings_layout.addWidget(QLabel("Apply to:"), row, 2)
        self.cmbOutlierApplyTo = QComboBox()
        self.cmbOutlierApplyTo.addItems(['CPM', 'DPM'])
        settings_layout.addWidget(self.cmbOutlierApplyTo, row, 3)
        row += 1
        
        # Activity Calculation
        settings_layout.addWidget(QLabel("<b>Activity Calculation</b>"), row, 0, 1, 4)
        row += 1
        
        settings_layout.addWidget(QLabel("Activity Unit:"), row, 0)
        self.cmbActivityUnit = QComboBox()
        self._populate_units()
        settings_layout.addWidget(self.cmbActivityUnit, row, 1)
        
        settings_layout.addWidget(QLabel("EF Method:"), row, 2)
        self.cmbEFMethod = QComboBox()
        self._populate_ef_methods()
        settings_layout.addWidget(self.cmbEFMethod, row, 3)
        row += 1
        
        # QC Thresholds
        settings_layout.addWidget(QLabel("<b>QC Thresholds</b>"), row, 0, 1, 4)
        row += 1
        
        settings_layout.addWidget(QLabel("Min Count Time (min):"), row, 0)
        self.spnMinCountTime = QDoubleSpinBox()
        self.spnMinCountTime.setRange(0, 1000)
        self.spnMinCountTime.setValue(30.0)
        settings_layout.addWidget(self.spnMinCountTime, row, 1)
        
        settings_layout.addWidget(QLabel("Max RSD %:"), row, 2)
        self.spnMaxRSD = QDoubleSpinBox()
        self.spnMaxRSD.setRange(0, 100)
        self.spnMaxRSD.setValue(5.0)
        settings_layout.addWidget(self.spnMaxRSD, row, 3)
        row += 1
        
        self.chkRequireQIP = QCheckBox("Require QIP Check")
        self.chkRequireQIP.setChecked(True)
        settings_layout.addWidget(self.chkRequireQIP, row, 0, 1, 2)
        
        settings_layout.setRowStretch(row + 1, 1)  # Push everything up
        tabs.addTab(settings_tab, "Calculation Settings")
        
        layout.addWidget(tabs)
        
        # Buttons
        btn_layout = QHBoxLayout()
        self.btnSave = QPushButton("Save Protocol")
        self.btnSave.clicked.connect(self._save_protocol)
        self.btnCancel = QPushButton("Cancel")
        self.btnCancel.clicked.connect(self.reject)
        
        btn_layout.addStretch()
        btn_layout.addWidget(self.btnSave)
        btn_layout.addWidget(self.btnCancel)
        layout.addLayout(btn_layout)
    
    def _populate_units(self):
        """Load activity units from database"""
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT UnitID, ShortName FROM MeasurementUnit WHERE UnitID < 5 ORDER BY UnitID"
                )).fetchall()
                for r in rows:
                    self.cmbActivityUnit.addItem(r.ShortName, r.UnitID)
        except Exception as e:
            logging.error(f"Failed to load units: {e}")
            self.cmbActivityUnit.addItems(['TU', 'DPM/kg', 'Bq/kg', 'pCi/L'])
    
    def _populate_ef_methods(self):
        """Load enrichment factor methods from database"""
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT ID, sName FROM GuitblEnrichmentFactorMethod WHERE ID IN (0,1,2,4) ORDER BY ID"
                )).fetchall()
                for r in rows:
                    self.cmbEFMethod.addItem(r.sName, r.ID)
        except Exception as e:
            logging.error(f"Failed to load EF methods: {e}")
            self.cmbEFMethod.addItems(['None', 'Deuterium', 'Spike', 'Manual'])
    
    def _on_bkg_mode_changed(self, mode):
        """Enable/disable manual background value"""
        self.spnBkgValue.setEnabled(mode == 'Manual')
    
    def _add_mapping_row(self):
        """Add a new empty mapping row with dropdowns populated from file headers"""
        row = self.tblMappings.rowCount()
        self.tblMappings.insertRow(row)
        
        # Target Field combo
        target_combo = QComboBox()
        target_combo.addItems(['CPM', 'DPM', 'QIP', 'Efficiency', 'Background', 'GrossCPM', 'GrossDPM'])
        self.tblMappings.setCellWidget(row, 0, target_combo)
        
        # Source Header combo/edit - POPULATE FROM FILE HEADERS
        source_combo = QComboBox()
        source_combo.setEditable(True)  # Allow custom entry if needed
        
        if self.available_headers:
            # Headers loaded from file
            source_combo.addItem("-- Select from file --", "")
            for header in self.available_headers:
                source_combo.addItem(header, header)
            source_combo.setToolTip("Headers loaded from selected file")
        else:
            # No file selected - allow manual entry
            source_combo.addItem("-- Type header name --", "")
            source_combo.setToolTip("No file selected. Type header name manually.")
        
        self.tblMappings.setCellWidget(row, 1, source_combo)
        
        # Is Net checkbox
        net_check = QCheckBox()
        net_widget = QWidget()
        net_layout = QHBoxLayout(net_widget)
        net_layout.addWidget(net_check)
        net_layout.setAlignment(Qt.AlignCenter)
        net_layout.setContentsMargins(0, 0, 0, 0)
        self.tblMappings.setCellWidget(row, 2, net_widget)
        
        # Uncertainty column - also populate from headers
        unc_combo = QComboBox()
        unc_combo.setEditable(True)
        
        if self.available_headers:
            unc_combo.addItem("-- None --", "")
            # Filter for uncertainty columns (containing "unc", "err", "std", "sigma")
            unc_headers = [h for h in self.available_headers 
                          if any(x in h.lower() for x in ['unc', 'err', 'std', 'sigma', 'error'])]
            for header in unc_headers:
                unc_combo.addItem(header, header)
            unc_combo.setToolTip("Uncertainty columns from file")
        else:
            unc_combo.addItem("-- None --", "")
            unc_combo.setToolTip("No file selected. Type header name manually.")
        
        self.tblMappings.setCellWidget(row, 3, unc_combo)
        
        # Order
        order_spin = QSpinBox()
        order_spin.setRange(0, 100)
        order_spin.setValue(row + 1)
        self.tblMappings.setCellWidget(row, 4, order_spin)
        
        # Notes
        self.tblMappings.setItem(row, 5, QTableWidgetItem(""))
    
    def _delete_mapping_row(self):
        """Delete selected mapping row"""
        current_row = self.tblMappings.currentRow()
        if current_row >= 0:
            self.tblMappings.removeRow(current_row)
    
    def _load_protocol_to_ui(self):
        """Load protocol data into UI controls"""
        self.txtName.setText(self.protocol.protocol_name)
        self.txtDesc.setPlainText(self.protocol.description)
        self.chkDefault.setChecked(self.protocol.is_default)
        self.chkActive.setChecked(self.protocol.is_active)
        
        # Load mappings
        self.tblMappings.setRowCount(0)
        for mapping in sorted(self.protocol.mappings, key=lambda m: m.display_order):
            row = self.tblMappings.rowCount()
            self.tblMappings.insertRow(row)
            
            # Target field
            target_combo = QComboBox()
            target_combo.addItems(['CPM', 'DPM', 'QIP', 'Efficiency', 'Background', 'GrossCPM', 'GrossDPM'])
            target_combo.setCurrentText(mapping.target_field)
            self.tblMappings.setCellWidget(row, 0, target_combo)
            
            # Source header - use combo box populated from file
            source_combo = QComboBox()
            source_combo.setEditable(True)
            
            if self.available_headers:
                source_combo.addItem("-- Select from file --", "")
                for header in self.available_headers:
                    source_combo.addItem(header, header)
                # Try to select the saved header
                idx = source_combo.findText(mapping.source_header)
                if idx >= 0:
                    source_combo.setCurrentIndex(idx)
                else:
                    # Header not in list - set as custom text
                    source_combo.setEditText(mapping.source_header)
            else:
                source_combo.addItem("-- Type header name --", "")
                source_combo.setEditText(mapping.source_header)
            
            self.tblMappings.setCellWidget(row, 1, source_combo)
            
            # Is Net
            net_check = QCheckBox()
            net_check.setChecked(mapping.is_net)
            net_widget = QWidget()
            net_layout = QHBoxLayout(net_widget)
            net_layout.addWidget(net_check)
            net_layout.setAlignment(Qt.AlignCenter)
            net_layout.setContentsMargins(0, 0, 0, 0)
            self.tblMappings.setCellWidget(row, 2, net_widget)
            
            # Uncertainty - also use combo
            unc_combo = QComboBox()
            unc_combo.setEditable(True)
            
            if self.available_headers:
                unc_combo.addItem("-- None --", "")
                unc_headers = [h for h in self.available_headers 
                              if any(x in h.lower() for x in ['unc', 'err', 'std', 'sigma', 'error'])]
                for header in unc_headers:
                    unc_combo.addItem(header, header)
                # Try to select saved uncertainty column
                if mapping.uncertainty_column:
                    idx = unc_combo.findText(mapping.uncertainty_column)
                    if idx >= 0:
                        unc_combo.setCurrentIndex(idx)
                    else:
                        unc_combo.setEditText(mapping.uncertainty_column)
            else:
                unc_combo.addItem("-- None --", "")
                if mapping.uncertainty_column:
                    unc_combo.setEditText(mapping.uncertainty_column)
            
            self.tblMappings.setCellWidget(row, 3, unc_combo)
            
            # Order
            order_spin = QSpinBox()
            order_spin.setRange(0, 100)
            order_spin.setValue(mapping.display_order)
            self.tblMappings.setCellWidget(row, 4, order_spin)
            
            # Notes
            self.tblMappings.setItem(row, 5, QTableWidgetItem(mapping.notes or ""))
        
        # Load settings
        s = self.protocol.settings
        self.cmbSignalMetric.setCurrentText(s.signal_metric)
        self.cmbEffSource.setCurrentText(s.efficiency_source)
        self.cmbBkgMode.setCurrentText(s.background_mode)
        if s.background_value is not None:
            self.spnBkgValue.setValue(s.background_value)
        self.cmbOutlierMethod.setCurrentText(s.outlier_method)
        self.spnOutlierThreshold.setValue(s.outlier_threshold)
        self.spnOutlierIter.setValue(s.outlier_iterations)
        self.cmbOutlierApplyTo.setCurrentText(s.outlier_apply_to)
        
        idx = self.cmbActivityUnit.findData(s.activity_unit)
        if idx >= 0:
            self.cmbActivityUnit.setCurrentIndex(idx)
        
        idx = self.cmbEFMethod.findData(s.enrichment_factor_method)
        if idx >= 0:
            self.cmbEFMethod.setCurrentIndex(idx)
        
        self.spnMinCountTime.setValue(s.min_count_time)
        self.spnMaxRSD.setValue(s.max_rsd)
        self.chkRequireQIP.setChecked(s.require_qip_check)
    
    def _save_protocol(self):
        """Save protocol from UI to database"""
        # Validate
        if not self.txtName.text().strip():
            QMessageBox.warning(self, "Validation", "Protocol name is required")
            return
        
        # Update protocol object
        self.protocol.protocol_name = self.txtName.text().strip()
        self.protocol.description = self.txtDesc.toPlainText().strip()
        self.protocol.is_default = self.chkDefault.isChecked()
        self.protocol.is_active = self.chkActive.isChecked()
        
        # Collect mappings
        self.protocol.mappings = []
        for row in range(self.tblMappings.rowCount()):
            target_combo = self.tblMappings.cellWidget(row, 0)
            source_combo = self.tblMappings.cellWidget(row, 1)  # CHANGED: Now combo box
            net_widget = self.tblMappings.cellWidget(row, 2)
            unc_combo = self.tblMappings.cellWidget(row, 3)     # CHANGED: Now combo box
            order_spin = self.tblMappings.cellWidget(row, 4)
            notes_item = self.tblMappings.item(row, 5)
            
            # Get source header from combo (either selected or typed)
            source_header = source_combo.currentText() if source_combo else ""
            if not source_header or source_header.startswith("--"):
                continue
            
            net_check = net_widget.layout().itemAt(0).widget()
            
            # Get uncertainty column from combo
            unc_header = unc_combo.currentText() if unc_combo else ""
            if unc_header and unc_header.startswith("--"):
                unc_header = None
            
            mapping = ColumnMapping(
                target_field=target_combo.currentText(),
                source_header=source_header.strip(),
                is_net=net_check.isChecked(),
                requires_background=not net_check.isChecked(),
                uncertainty_column=unc_header.strip() if unc_header else None,
                display_order=order_spin.value(),
                notes=notes_item.text().strip() if notes_item else None
            )
            self.protocol.mappings.append(mapping)
        
        # Collect settings
        self.protocol.settings = ProtocolSettings(
            signal_metric=self.cmbSignalMetric.currentText(),
            efficiency_source=self.cmbEffSource.currentText(),
            background_mode=self.cmbBkgMode.currentText(),
            background_value=self.spnBkgValue.value() if self.cmbBkgMode.currentText() == 'Manual' else None,
            outlier_method=self.cmbOutlierMethod.currentText(),
            outlier_threshold=self.spnOutlierThreshold.value(),
            outlier_iterations=self.spnOutlierIter.value(),
            outlier_apply_to=self.cmbOutlierApplyTo.currentText(),
            activity_unit=self.cmbActivityUnit.currentData() or 1,
            enrichment_factor_method=self.cmbEFMethod.currentData() or 2,
            min_count_time=self.spnMinCountTime.value(),
            max_rsd=self.spnMaxRSD.value(),
            require_qip_check=self.chkRequireQIP.isChecked()
        )
        
        # Save to database
        try:
            protocol_id = LSCProtocolManager.save_protocol(self.protocol)
            QMessageBox.information(self, "Success", f"Protocol saved (ID: {protocol_id})")
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save protocol: {e}")
            logging.error(f"Protocol save failed: {e}", exc_info=True)


class ProtocolManagerDialog(QDialog):
    """Main dialog for managing LSC protocols"""
    
    def __init__(self, parent=None, isotope_id: int = 200, file_format_id: int = 6):
        super().__init__(parent)
        self.isotope_id = isotope_id
        self.file_format_id = file_format_id
        self.selected_protocol = None
        self.parent_window = parent  # Store parent reference to get file path
        
        self.setWindowTitle('LSC Protocol Manager')
        self.setMinimumSize(800, 600)
        self._init_ui()
        self._select_format_and_isotope(isotope_id, file_format_id)
        self._refresh_protocol_list()
    
    def _init_ui(self):
        layout = QVBoxLayout(self)
        
        # Filter controls
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("Isotope:"))
        self.cmbIsotope = QComboBox()
        self._populate_isotopes()
        self.cmbIsotope.currentIndexChanged.connect(self._refresh_protocol_list)
        filter_layout.addWidget(self.cmbIsotope)
        
        filter_layout.addWidget(QLabel("Format:"))
        self.cmbFormat = QComboBox()
        self._populate_formats()
        self.cmbFormat.currentIndexChanged.connect(self._refresh_protocol_list)
        filter_layout.addWidget(self.cmbFormat)
        
        filter_layout.addStretch()
        layout.addLayout(filter_layout)
        
        # Protocol list
        self.lstProtocols = QListWidget()
        self.lstProtocols.itemDoubleClicked.connect(self._edit_protocol)
        layout.addWidget(self.lstProtocols)
        
        # Buttons
        btn_layout = QHBoxLayout()
        self.btnNew = QPushButton("New Protocol")
        self.btnNew.clicked.connect(self._new_protocol)
        self.btnEdit = QPushButton("Edit")
        self.btnEdit.clicked.connect(self._edit_protocol)
        self.btnCopy = QPushButton("Copy")
        self.btnCopy.clicked.connect(self._copy_protocol)
        self.btnDelete = QPushButton("Delete")
        self.btnDelete.clicked.connect(self._delete_protocol)
        self.btnSelect = QPushButton("Select & Close")
        self.btnSelect.clicked.connect(self._select_protocol)
        self.btnClose = QPushButton("Close")
        self.btnClose.clicked.connect(self.reject)
        
        btn_layout.addWidget(self.btnNew)
        btn_layout.addWidget(self.btnEdit)
        btn_layout.addWidget(self.btnCopy)
        btn_layout.addWidget(self.btnDelete)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btnSelect)
        btn_layout.addWidget(self.btnClose)
        layout.addLayout(btn_layout)

    def _select_format_and_isotope(self, isotope_id: int, file_format_id: int):
        """Pre-select format and isotope in dropdowns"""
        try:
            # Select isotope
            idx = self.cmbIsotope.findData(isotope_id)
            if idx >= 0:
                self.cmbIsotope.setCurrentIndex(idx)
            
            # Select format
            idx = self.cmbFormat.findData(file_format_id)
            if idx >= 0:
                self.cmbFormat.setCurrentIndex(idx)
            
            logging.info(f"Pre-selected Isotope {isotope_id}, Format {file_format_id}")
        except Exception as e:
            logging.error(f"Failed to pre-select format/isotope: {e}")
                
    def _populate_isotopes(self):
        """Load isotopes from database"""
        # Simplified - in real app, load from database
        self.cmbIsotope.addItem("H-3", 200)
        self.cmbIsotope.addItem("C-14", 201)
        self.cmbIsotope.addItem("Sr-90", 202)
    
    def _populate_formats(self):
        """Load file formats from database"""
        # Simplified - in real app, load from database
        formats = [
            ("Quantulus (Window)", 1),
            ("Quantulus (List)", 2),
            ("HIDEX Matrix", 6),
            ("HIDEX List", 12),
            ("Packard", 3),
            ("PerkinElmer", 4),
            ("Aloka", 7)
        ]
        for name, fmt_id in formats:
            self.cmbFormat.addItem(name, fmt_id)
    
    def _refresh_protocol_list(self):
        """Reload protocol list based on filters"""
        self.lstProtocols.clear()
        
        isotope_id = self.cmbIsotope.currentData()
        format_id = self.cmbFormat.currentData()
        
        if isotope_id is None or format_id is None:
            return
        
        try:
            protocols = LSCProtocolManager.list_protocols(isotope_id, format_id)
            for pid, name, desc in protocols:
                item = QListWidgetItem(f"{name}")
                if desc:
                    item.setToolTip(desc)
                item.setData(Qt.UserRole, pid)
                
                # Bold font for default
                try:
                    protocol = LSCProtocolManager.load_protocol(pid)
                    if protocol and protocol.is_default:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
                
                self.lstProtocols.addItem(item)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load protocols: {e}")
            logging.error(f"Protocol list refresh failed: {e}", exc_info=True)
    
    def _new_protocol(self):
        """Create new protocol"""
        isotope_id = self.cmbIsotope.currentData()
        format_id = self.cmbFormat.currentData()
        
        if isotope_id is None or format_id is None:
            QMessageBox.warning(self, "Selection Required", "Please select isotope and format")
            return
        
        # Get available headers from parent window's already-populated combos
        available_headers = []
        if hasattr(self.parent_window, '_get_available_headers'):
            available_headers = self.parent_window._get_available_headers()
            logging.info(f"Got {len(available_headers)} headers from parent window")
        
        dlg = ProtocolEditorDialog(
            self, 
            isotope_id=isotope_id, 
            file_format_id=format_id,
            available_headers=available_headers
        )
        if dlg.exec_() == QDialog.Accepted:
            self._refresh_protocol_list()
    
    def _edit_protocol(self):
        """Edit selected protocol"""
        item = self.lstProtocols.currentItem()
        if not item:
            return
        
        protocol_id = item.data(Qt.UserRole)
        protocol = LSCProtocolManager.load_protocol(protocol_id)
        
        if protocol:
            # Get available headers from parent window's already-populated combos
            available_headers = []
            if hasattr(self.parent_window, '_get_available_headers'):
                available_headers = self.parent_window._get_available_headers()
            
            dlg = ProtocolEditorDialog(
                self, 
                protocol=protocol,
                available_headers=available_headers
            )
            if dlg.exec_() == QDialog.Accepted:
                self._refresh_protocol_list()
    
    def _copy_protocol(self):
        """Copy selected protocol"""
        item = self.lstProtocols.currentItem()
        if not item:
            return
        
        protocol_id = item.data(Qt.UserRole)
        original = LSCProtocolManager.load_protocol(protocol_id)
        
        if original:
            copy = LSCProtocol(
                protocol_name=f"{original.protocol_name} (Copy)",
                isotope_id=original.isotope_id,
                file_format_id=original.file_format_id,
                description=original.description,
                is_default=False,
                mappings=original.mappings.copy(),
                settings=original.settings
            )
            
            dlg = ProtocolEditorDialog(self, protocol=copy)
            if dlg.exec_() == QDialog.Accepted:
                self._refresh_protocol_list()
    
    def _delete_protocol(self):
        """Delete selected protocol"""
        item = self.lstProtocols.currentItem()
        if not item:
            return
        
        protocol_id = item.data(Qt.UserRole)
        protocol = LSCProtocolManager.load_protocol(protocol_id)
        
        if not protocol:
            return
        
        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Delete protocol '{protocol.protocol_name}'?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            try:
                protocol.is_active = False
                LSCProtocolManager.save_protocol(protocol)
                self._refresh_protocol_list()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to delete: {e}")
    
    def _select_protocol(self):
        """Select protocol and close"""
        item = self.lstProtocols.currentItem()
        if not item:
            QMessageBox.warning(self, "Selection Required", "Please select a protocol")
            return
        
        protocol_id = item.data(Qt.UserRole)
        self.selected_protocol = LSCProtocolManager.load_protocol(protocol_id)
        self.accept()
    
    def get_selected_protocol(self) -> Optional[LSCProtocol]:
        """Get the selected protocol after dialog closes"""
        return self.selected_protocol


if __name__ == '__main__':
    import sys
    from PyQt5.QtWidgets import QApplication
    
    app = QApplication(sys.argv)
    dlg = ProtocolManagerDialog()
    dlg.show()
    sys.exit(app.exec_())