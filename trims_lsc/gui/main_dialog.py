"""
TRIMS LSC Import Dialog - Main GUI

Extracted from trims_lsc_details_gui.py
Date: 2026-02-08 10:52:29
"""

from __future__ import annotations  # Allow forward references in type hints

import sys
import logging
import pandas as pd
import numpy as np
import os
import re
from typing import Optional, List, Dict, Tuple, TYPE_CHECKING
from datetime import datetime

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from PyQt5.QtWidgets import *
from PyQt5.QtGui import *
from PyQt5.QtCore import *
from PyQt5.QtPrintSupport import QPrinter, QPrintDialog

from ..parsers import make_lsc_parser
from ..workers import LSCImportWorker
from ..computation import compute_means, compute_run_params, _compute_net_activity_dpm_row

from db_core import db_manager
from sqlalchemy import text
from shared_utils import *

from protocol_manager import ProtocolManager, Protocol, ColumnMapping
from protocol_gui import ProtocolEditorDialog, ProtocolGUI

# Import helper functions and dialogs
from .helpers import (
    _canon_quantulus_source,
    HidexMatrixMappingDialog,
    save_hidex_me_to_db,
    calculate_and_save_final_activities,
    _get_countid_map,
    _bulk_delete_run_mean_and_raw,
)

from ..parsers.quantulus import parse_windows

from smart_formatting import (
    format_for_database,
    format_value_uncertainty,
    format_snapshot_parameters,
    format_chi_squared
)

class TrimsLSCImportDialog(QDialog):
    def __init__(self, run_id, parent=None):
        super().__init__(parent)
        self.run_id = run_id
        self.setWindowTitle(f'Import LSC Data — Run {self.run_id}')
        self.resize(1280, 880)

        # State
        self.raw_data = None
        self.current_pos = None
        self.computed_means = None 
        self.bkg_mean = 0.0        
        self.bkg_unc = 0.0         
        self.eff = 0.0             
        self.eff_unc = 0.0         
        self.chi_sq = 0.0          
        self._quantulus_windows = []
        self.file_headers = None
        self.equipment_id, self.equipment_name, self.model_name = get_equipment_info(self.run_id)
        self.current_user = get_current_user_id()
        
        # UI
        self._init_ui()
        self._load_formats()
        self._load_defaults_from_global()
        self._load_outlier_prefs()
        self._populate_calc_combos()
        
        self.cmbActivityUnit.currentIndexChanged.connect(self._populate_preview_table)
        self.cmbEFMethod.currentIndexChanged.connect(self._populate_preview_table)
        self._update_eff_pct_toggle_visibility()
        self.raise_(); self.activateWindow()

        # Connect format combo to auto-load default protocol
        self.cmbFormat.currentIndexChanged.connect(self._on_format_changed)

    def _reset_ui_to_defaults(self):
        """
        Reset ALL UI elements to match initial load-time state.
        Called when format changes or new file is browsed.
        """
        # Clear all data
        self.raw_data = None
        self.current_pos = None
        self.computed_means = None
        self.bkg_mean = 0.0
        self.bkg_unc = 0.0
        self.eff = 0.0
        self.eff_unc = 0.0
        self.chi_sq = 0.0
        self._quantulus_windows = []
        self.file_headers = None
        
        # Clear file path
        if hasattr(self, 'txtPath'):
            self.txtPath.clear()
        
        # Clear all tables
        if hasattr(self, 'modPreview') and self.modPreview:
            self.modPreview.clear()
            self.modPreview.setRowCount(0)
            self.modPreview.setColumnCount(0)
        
        if hasattr(self, 'modelVial') and self.modelVial:
            self.modelVial.clear()
            self.modelVial.setRowCount(0)
            self.modelVial.setColumnCount(0)
        
        # Reset all tabs to first tab
        if hasattr(self, 'tabsPreview') and self.tabsPreview:
            self.tabsPreview.setCurrentIndex(0)
        
        if hasattr(self, 'tabsPlots') and self.tabsPlots:
            self.tabsPlots.setCurrentIndex(0)
        
        # Clear all plots
        if hasattr(self, 'ax') and self.ax:
            self.ax.clear()
            self.ax.set_xlabel('Position')
            self.ax.set_ylabel('CPM')
            self.ax.set_title('LSC Data Preview')
            self.ax.grid(True, alpha=0.3)
            if hasattr(self, 'canvas'):
                self.canvas.draw()
        
        # Clear vial plot
        if hasattr(self, 'vial_plot_canvas') and self.vial_plot_canvas:
            fig = self.vial_plot_canvas.figure
            fig.clear()
            self.vial_plot_canvas.draw()
        
        # Clear calibration plot
        if hasattr(self, 'calib_plot_canvas') and self.calib_plot_canvas:
            fig = self.calib_plot_canvas.figure
            fig.clear()
            self.calib_plot_canvas.draw()
        
        # Reset mapping combo boxes
        for combo in [self.cmbWinCPM, self.cmbWinDPM, self.cmbWinQIP, self.cmbWinFull]:
            if combo:
                combo.clear()
                combo.addItem('-- Select Source --', None)
        
        # Reset plot field combo
        if hasattr(self, 'cmbCPMForPlot') and self.cmbCPMForPlot:
            self.cmbCPMForPlot.clear()
            self.cmbCPMForPlot.addItem('-- Select Field --', None)
        
        if hasattr(self, 'cmbPlotCPM') and self.cmbPlotCPM:
            self.cmbPlotCPM.clear()
            self.cmbPlotCPM.addItem('-- Select Field --', None)
        
        # Reset NET checkboxes
        if hasattr(self, 'chkCpmNet'):
            self.chkCpmNet.setChecked(False)
        if hasattr(self, 'chkDpmNet'):
            self.chkDpmNet.setChecked(False)
        if hasattr(self, 'chkQipNet'):
            self.chkQipNet.setChecked(False)
        if hasattr(self, 'chkFullNet'):
            self.chkFullNet.setChecked(False)
        
        # Reset count time unit to minutes (default)
        if hasattr(self, 'cmbCountTimeUnit') and self.cmbCountTimeUnit:
            self.cmbCountTimeUnit.setCurrentIndex(0)
        
        # Reset outlier controls
        if hasattr(self, 'chkApplyOutliers') and self.chkApplyOutliers:
            self.chkApplyOutliers.setChecked(False)
        
        if hasattr(self, 'cmbOutlierMethod') and self.cmbOutlierMethod:
            self.cmbOutlierMethod.setCurrentIndex(0)
        
        if hasattr(self, 'spnThreshold') and self.spnThreshold:
            self.spnThreshold.setValue(2.0)
        
        if hasattr(self, 'spnBandSigma') and self.spnBandSigma:
            self.spnBandSigma.setValue(2.0)
        
        # Reset calculation combos
        if hasattr(self, 'cmbEFMethod') and self.cmbEFMethod:
            self.cmbEFMethod.setCurrentIndex(0)
        
        if hasattr(self, 'cmbActivityUnit') and self.cmbActivityUnit:
            self.cmbActivityUnit.setCurrentIndex(0)
        
        # Clear status
        if hasattr(self, 'lblStatus'):
            self.lblStatus.setText("Ready - select format and browse file")
        
        # Reset progress bar
        if hasattr(self, 'progressBar'):
            self.progressBar.setValue(0)
            self.progressBar.setVisible(False)
        
        # Disable buttons (no data loaded)
        if hasattr(self, 'btnCompute'):
            self.btnCompute.setEnabled(False)
        
        if hasattr(self, 'btnSave'):
            self.btnSave.setEnabled(False)
        
        if hasattr(self, 'btnPrint'):
            self.btnPrint.setEnabled(False)

    def _on_format_changed(self):
        """
        When user changes file format, load default protocol for that format.
        
        REFACTORED: Now filters by format_id for format-specific defaults
        """
        # RESET UI to defaults first
        self._reset_ui_to_defaults()
        
        # Get current format ID
        format_id = None
        if hasattr(self, 'cmbFormat'):
            format_id = self.cmbFormat.currentData()
            if format_id is not None:
                format_id = int(format_id)
        
        # Get default LSC protocol for this specific format
        try:
            if format_id:
                # Try format-specific default first
                protocol = ProtocolManager.get_default_protocol(
                    module='LSC',
                    file_format_id=format_id  # ← CRITICAL: Filter by format!
                )
                
                if protocol:
                    # Apply the complete protocol (includes cal settings and windows)
                    self._apply_protocol(protocol)
                    self.lblCurrentProtocol.setText(f"Protocol: {protocol.name} (Default)")
                    logging.info(f"Auto-loaded default LSC protocol for format {format_id}: {protocol.name}")
                    
                    # Explicitly apply calculation settings
                    self._apply_protocol_calc_settings(protocol.settings)
                    
                    # Explicitly apply windows/mappings (if format-specific handling needed)
                    self._apply_format_specific_mappings(protocol, format_id)
                    return
            
            # Fallback: Try module-level default (any format)
            protocol = ProtocolManager.get_default_protocol(module='LSC')
            
            if protocol:
                self._apply_protocol(protocol)
                self.lblCurrentProtocol.setText(f"Protocol: {protocol.name} (Default)")
                logging.info(f"Auto-loaded default LSC protocol: {protocol.name}")
                
                # Apply settings
                self._apply_protocol_calc_settings(protocol.settings)
                
                if format_id:
                    self._apply_format_specific_mappings(protocol, format_id)
            else:
                logging.info("No default LSC protocol found, using UI defaults")
                self.lblCurrentProtocol.setText("Protocol: None (Using UI defaults)")

        except Exception as e:
            logging.error(f"Failed to load default protocol: {e}", exc_info=True)
            QMessageBox.warning(self, "Protocol Load Error", 
                            f"Could not load default protocol:\n{e}\n\nUsing default UI settings.")
            self.lblCurrentProtocol.setText("Protocol: Error")

    def _apply_protocol_calc_settings(self, settings_dict: dict):
        """
        Apply protocol calculation settings to UI.
        
        REFACTORED: Takes dict instead of ProtocolSettings object
        
        Args:
            settings_dict: Protocol.settings dictionary
        """
        # Signal metric (new key: signal_metric; old key: metric)
        metric = settings_dict.get("signal_metric") or settings_dict.get("metric", "CPM")
        idx = self.cmbSignalMetric.findText(metric)
        if idx >= 0:
            self.cmbSignalMetric.setCurrentIndex(idx)

        # Efficiency source (new key: efficiency_source; old key: efficiency_mode)
        eff_source = settings_dict.get("efficiency_source") or settings_dict.get("efficiency_mode", "Per-run (computed)")
        idx = self.cmbEffSource.findText(eff_source)
        if idx >= 0:
            self.cmbEffSource.setCurrentIndex(idx)

        bkg_source = settings_dict.get("background_mode", "Calculated")
        idx = self.cmbBgdSource.findText(bkg_source)
        if idx >= 0:
            self.cmbBgdSource.setCurrentIndex(idx)

        # Outlier method
        outlier = settings_dict.get("outlier_method", "Chauvenet")
        idx = self.cmbOutlier.findText(outlier)
        if idx >= 0:
            self.cmbOutlier.setCurrentIndex(idx)

        # Outlier threshold (new key: outlier_threshold; old key: outlier_sigma)
        threshold = settings_dict.get("outlier_threshold") or settings_dict.get("outlier_sigma", 2.0)
        self.spnThreshold.setValue(float(threshold))

        # Activity unit — stored as int (canonical) or legacy string ("TU" etc.)
        activity_unit = settings_dict.get("activity_unit", 1)
        try:
            idx = self.cmbActivityUnit.findData(int(activity_unit))
        except (TypeError, ValueError):
            idx = -1
        if idx < 0:
            idx = self.cmbActivityUnit.findText(str(activity_unit))
        if idx >= 0:
            self.cmbActivityUnit.setCurrentIndex(idx)

        # EF method — stored as int (canonical) or legacy string ("1 --- Deuterium" etc.)
        ef_method = settings_dict.get("enrichment_factor_method", 1)
        try:
            idx = self.cmbEFMethod.findData(int(ef_method))
        except (TypeError, ValueError):
            idx = -1
        if idx < 0:
            idx = self.cmbEFMethod.findText(str(ef_method))
        if idx >= 0:
            self.cmbEFMethod.setCurrentIndex(idx)
        
        logging.info(f"Applied protocol settings: {metric}, {eff_source}, {outlier}")


    def _apply_format_specific_mappings(self, protocol, format_id: int):
        """Apply window mappings from protocol for the given file format."""
        self._apply_protocol_mappings(protocol.mappings)

    def _apply_protocol_mappings(self, mappings: List["ColumnMapping"]):
        """Apply window mappings from protocol to UI combos"""
        try:
            for mapping in mappings:
                if mapping.target_field == 'CPM' and mapping.source_header:
                    idx = self.cmbWinCPM.findText(mapping.source_header)
                    if idx >= 0:
                        self.cmbWinCPM.setCurrentIndex(idx)
                    # Apply NET checkbox
                    if hasattr(self, 'chkNetCPM'):
                        self.chkNetCPM.setChecked(mapping.is_net)
                
                elif mapping.target_field == 'DPM' and mapping.source_header:
                    idx = self.cmbWinDPM.findText(mapping.source_header)
                    if idx >= 0:
                        self.cmbWinDPM.setCurrentIndex(idx)
                    if hasattr(self, 'chkNetDPM'):
                        self.chkNetDPM.setChecked(mapping.is_net)
                
                elif mapping.target_field == 'QIP' and mapping.source_header:
                    idx = self.cmbWinQIP.findText(mapping.source_header)
                    if idx >= 0:
                        self.cmbWinQIP.setCurrentIndex(idx)
                
                elif mapping.target_field == 'Full' and mapping.source_header:
                    idx = self.cmbWinFull.findText(mapping.source_header)
                    if idx >= 0:
                        self.cmbWinFull.setCurrentIndex(idx)
            
            logging.info(f"Applied protocol window mappings")
        except Exception as e:
            logging.error(f"Failed to apply protocol mappings: {e}")

    def _save_protocol_snapshot_after_processing(self):
        """
        Save complete protocol snapshot after data processing.
        Should be called after compute_run_params() completes.
        
        NEW: Uses unified ProtocolManager.save_run_protocol_snapshot()
        """
        try:
            protocol = self._get_current_protocol()
            
            # Build computed parameters for snapshot
            computed_params = {
                'background_cpm': float(self.bkg_mean),
                'background_cpm_uncertainty': float(self.bkg_unc),
                'efficiency': float(self.eff),
                'efficiency_uncertainty': float(self.eff_unc),
                'chi_squared': float(self.chi_sq),
                'outliers_removed': getattr(self, 'outlier_count', 0),
                'samples_processed': len(self.computed_means) if getattr(self.computed_means, "empty", False) is False else 0
            }
            
            # Save snapshot with computed parameters
            ProtocolManager.save_run_protocol_snapshot(
                run_id=self.run_id,
                protocol=protocol,
                module='LSC',
                fit_parameters=computed_params,
                was_modified = getattr(self, "protocol_was_modified", False) is True,
                user=get_current_user_id() or 'GUI_USER'
            )
            
            logging.info(f"Saved protocol snapshot for run {self.run_id}")
            
        except Exception as e:
            logging.error(f"Failed to save protocol snapshot: {e}", exc_info=True)
            
    def _load_format_defaults(self, format_id: int, isotope_id: int):
        """
        Fallback: Load default settings from database when no protocol exists.
        This loads saved UI preferences for this format-isotope combination.
        """
        try:
            # Load saved mapping
            saved_mapping = self._get_saved_mapping(format_id, isotope_id)
            
            # Load saved preferences
            prefs = self._load_outlier_prefs()  # Your existing method
            
            # Apply if found
            if saved_mapping:
                # Apply windows from saved mapping
                # (This is your existing logic)
                pass
            
            logging.info(f"Loaded format defaults for Format {format_id}")
        except Exception as e:
            logging.error(f"Failed to load format defaults: {e}")
            
    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        
        # Create horizontal layout for collapse button + splitter
        content_layout = QHBoxLayout()
        
        # Collapse/Expand button (sidebar toggle)
        self.btnToggleSidebar = QPushButton('◀')  # Unicode left arrow
        self.btnToggleSidebar.setMaximumWidth(30)
        self.btnToggleSidebar.setMinimumHeight(40)
        self.btnToggleSidebar.setToolTip('Collapse/Expand Controls Panel')
        self.btnToggleSidebar.clicked.connect(self._toggle_sidebar)
        self.btnToggleSidebar.setStyleSheet("""
            QPushButton {
                background-color: #3498db;
                color: white;
                border: none;
                font-size: 16px;
                font-weight: bold;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #2980b9;
            }
            QPushButton:pressed {
                background-color: #1f618d;
            }
        """)
        content_layout.addWidget(self.btnToggleSidebar)
        
        splitter = QSplitter(Qt.Horizontal)
        self.splitter = splitter  # Store reference for collapse

        # LEFT PANEL
        left_w = QWidget(); left_l = QVBoxLayout(left_w); left_l.setContentsMargins(0,0,0,0)
        self.left_panel = left_w  # Store reference for collapse

        # Equipment
        eg = QGroupBox('Equipment')
        el = QGridLayout()
        self.lblEquip = QLabel(f"ID: {self.equipment_id or '-'}  {self.equipment_name or ''} ({self.model_name or ''})")
        el.addWidget(self.lblEquip, 0, 0)
        eg.setLayout(el); left_l.addWidget(eg)

        # 1) Data Source
        fg = QGroupBox('1. Data Source')
        fl = QGridLayout()
        self.txtPath = QLineEdit(); self.btnBrowse = QPushButton('…'); self.btnBrowse.clicked.connect(self._browse_file)
        self.cmbFormat = QComboBox()
        fl.addWidget(QLabel('Format:'), 0, 0); fl.addWidget(self.cmbFormat, 0, 1, 1, 2)
        fl.addWidget(QLabel('File:'),   1, 0); fl.addWidget(self.txtPath, 1, 1); fl.addWidget(self.btnBrowse, 1, 2)
        fg.setLayout(fl); left_l.addWidget(fg)

        # 2) Windows

        wg = QGroupBox('2. Windows')
        wl = QGridLayout(wg)

        # --- Combos ---
        self.cmbWinCPM  = QComboBox()
        self.cmbWinDPM  = QComboBox()
        self.cmbWinFull = QComboBox()
        self.cmbWinQIP  = QComboBox()

        # Optional: ensure decent width for readability
        for cb in [self.cmbWinCPM, self.cmbWinDPM, self.cmbWinFull, self.cmbWinQIP]:
            cb.setMinimumWidth(120)

        # --- Row 0: CPM + DPM (label, combo, Net) ---
        wl.addWidget(QLabel('CPM:'),           0, 0)
        wl.addWidget(self.cmbWinCPM,           0, 1)
        self.chkNetCPM = QCheckBox('Net')
        wl.addWidget(self.chkNetCPM,           0, 2, alignment=Qt.AlignLeft | Qt.AlignVCenter)
        wl.addWidget(QLabel('DPM:'),           0, 3)
        wl.addWidget(self.cmbWinDPM,           0, 4)
        self.chkNetDPM = QCheckBox('Net')
        wl.addWidget(self.chkNetDPM,           0, 5, alignment=Qt.AlignLeft | Qt.AlignVCenter)
        # --- Row 1: Full + QIP (label, combo, Net) ---
        wl.addWidget(QLabel('Full:'),          1, 0)
        wl.addWidget(self.cmbWinFull,          1, 1)
        self.chkNetFull = QCheckBox('Net')
        wl.addWidget(self.chkNetFull,          1, 2, alignment=Qt.AlignLeft | Qt.AlignVCenter)
        wl.addWidget(QLabel('QIP:'),           1, 3)
        wl.addWidget(self.cmbWinQIP,           1, 4)
        # --- Row 2 (last row): Count time + Edit Mapping, aligned with the groups above ---
        self.cmbCountTimeUnit = QComboBox()
        self.cmbCountTimeUnit.addItems(['Minutes', 'Seconds'])
        wl.addWidget(QLabel('Time in:'), 2, 0)
        wl.addWidget(self.cmbCountTimeUnit,    2, 1)

        wl.setColumnStretch(1, 1)
        wl.setColumnStretch(4, 1)
        wl.setColumnStretch(5, 0)
        wl.setHorizontalSpacing(12)
        wl.setVerticalSpacing(8)
        wl.setContentsMargins(8, 8, 8, 8)
        wg.setLayout(wl)
        left_l.addWidget(wg)
        self.cmbWinCPM.currentIndexChanged.connect(self._on_cpm_window_changed)

        # 3) Outlier Detection
        og = QGroupBox('3. Outlier Detection')
        ol = QGridLayout()
        
        self.cmbOutlier = QComboBox()
        self.cmbOutlier.addItems([
            'None', 
            'Chauvenet',
            'Modified Z-Score', 
            'X_STDV',
            'Grubbs',
            'Dixon',
            'GESD'
        ])
        self.cmbOutlier.setToolTip(
            "Outlier Detection Methods:\n\n"
            "• None: No detection\n"
            "• Chauvenet: Probabilistic criterion (recommended)\n"
            "• Modified Z-Score: MAD-based (very robust)\n"
            "• X_STDV: Flexible sigma threshold\n"
            "    Set threshold to 2.0 (≈2σ), 2.5, 3.0 (≈3σ), etc.\n"
            "• Grubbs: Single outlier test\n"
            "• Dixon: Q-test for small samples (n<10)\n"
            "• GESD: Multiple outliers (max = threshold)"
        )
        
        self.spnThreshold = QDoubleSpinBox(); self.spnThreshold.setRange(0.1, 20.0); self.spnThreshold.setValue(2.0)
        self.spnBandSigma = QDoubleSpinBox(); self.spnBandSigma.setRange(0.5, 10.0); self.spnBandSigma.setSingleStep(0.5); self.spnBandSigma.setValue(2.0)
        self.spnBandSigma.valueChanged.connect(self._update_plot)
        ol.addWidget(QLabel('Method:'), 0, 0); ol.addWidget(self.cmbOutlier, 0, 1)
        ol.addWidget(QLabel('Sigma / Max N:'), 1, 0); ol.addWidget(self.spnThreshold, 1, 1)
        ol.addWidget(QLabel('Plot σ:'), 2, 0); ol.addWidget(self.spnBandSigma, 2, 1)
        og.setLayout(ol); left_l.addWidget(og)

        self.cmbFormat.currentIndexChanged.connect(self._update_eff_pct_toggle_visibility)
        self.cmbOutlier.currentIndexChanged.connect(self._on_compute_clicked)
        self.spnThreshold.valueChanged.connect(self._on_compute_clicked)  
              
        # 2. Calculation & Enrichment Settings
        cg = QGroupBox('2. Calculation Settings')
        cl = QGridLayout()
        self.cmbActivityUnit = QComboBox()
        self.cmbEFMethod = QComboBox()
        cl.addWidget(QLabel('Target Unit:'), 0, 0); cl.addWidget(self.cmbActivityUnit, 0, 1)
        cl.addWidget(QLabel('EF Method:'), 1, 0); cl.addWidget(self.cmbEFMethod, 1, 1)

        # Signal metric
        self.cmbSignalMetric = QComboBox()
        self.cmbSignalMetric.addItems(["CPM", "DPM"])
        cl.addWidget(QLabel("Signal metric:"), 2, 0)
        cl.addWidget(self.cmbSignalMetric, 2, 1)

        # Efficiency source (used only if DPM source = compute OR when deriving CPM from DPM for preview)
        self.cmbEffSource = QComboBox()
        self.cmbEffSource.addItems(["Per-cycle (file)", "Per-run (computed)", "Per-run (file)"])
        cl.addWidget(QLabel("Efficiency source:"), 3, 0)
        cl.addWidget(self.cmbEffSource, 3, 1)

        # Efficiency source (used only if DPM source = compute OR when deriving CPM from DPM for preview)
        self.cmbBgdSource = QComboBox()
        self.cmbBgdSource.addItems(["UseFile", "Manual", "Calculated"])
        cl.addWidget(QLabel("Background Mode:"), 4, 0)
        cl.addWidget(self.cmbBgdSource, 4, 1)
        
        self.btnManageProtocols = QPushButton("Manage Protocols")
        self.btnManageProtocols.clicked.connect(self._manage_protocols)
        cl.addWidget(self.btnManageProtocols, 5, 0)

        self.btnSelectProtocol = QPushButton("Load Protocol...")
        self.btnSelectProtocol.clicked.connect(self._select_protocol)
        cl.addWidget(self.btnSelectProtocol, 5, 1)

        self.lblCurrentProtocol = QLabel("Protocol: <Default>")
        self.lblCurrentProtocol.setStyleSheet("color: #666; font-style: italic; font-size: 9pt;")
        cl.addWidget(self.lblCurrentProtocol, 5, 2, 1, 2)

        cg.setLayout(cl); left_l.addWidget(cg)
        
        # Progress + Status
        self.progressBar = QProgressBar(); self.progressBar.setAlignment(Qt.AlignCenter)
        left_l.addWidget(self.progressBar)
        self.lblStatus = QLabel('Ready'); left_l.addWidget(self.lblStatus)

        # STEP buttons with styled simple symbols (emoji don't render in color on Windows Qt)
        self.btnImport  = QPushButton('⬇ Import Raw Data')  # Down arrow
        self.btnImport.setStyleSheet('font-weight: bold; background-color: #3498db; color: white; padding: 8px;')
        self.btnImport.clicked.connect(self._start_import)
        
        self.btnCompute = QPushButton('⚙ Compute Results')  # Gear
        self.btnCompute.setStyleSheet('font-weight: bold; background-color: #9b59b6; color: white; padding: 8px;')
        self.btnCompute.setEnabled(False)
        self.btnCompute.clicked.connect(self._on_compute_clicked)
        
        self.btnSaveRun = QPushButton('OK Save All to DB')  # Checkmark
        self.btnSaveRun.setStyleSheet('font-weight: bold; background-color: #27ae60; color: white; padding: 8px;')
        self.btnSaveRun.setEnabled(False)
        self.btnSaveRun.clicked.connect(self._save_run)
        
        btn_close = QPushButton('✕ Close')  # X mark
        btn_close.setStyleSheet('font-weight: bold; background-color: #e74c3c; color: white; padding: 8px;')
        btn_close.clicked.connect(self.reject)

        bl = QHBoxLayout()
        bl.addWidget(self.btnImport); bl.addWidget(self.btnCompute); bl.addWidget(self.btnSaveRun); bl.addWidget(btn_close)
        left_l.addLayout(bl)

        # Print Button
        self.btnPrintQC = QPushButton("🖨 Print QC Report")  # Printer
        self.btnPrintQC.setEnabled(False)
        self.btnPrintQC.clicked.connect(self._print_qc_report)
        left_l.addWidget(self.btnPrintQC)
        
        splitter.addWidget(left_w)

        # RIGHT PANEL (plot + nav + table)
        right_w = QWidget(); rl = QVBoxLayout(right_w)
        self.tabsPlots = QTabWidget()
        rl.addWidget(self.tabsPlots)
        # Tab 1: Individual Vial Cycles
        self.vial_plot_tab = QWidget()
        vial_layout = QVBoxLayout(self.vial_plot_tab)
        self.vial_plot_canvas = FigureCanvas(Figure())
        vial_layout.addWidget(self.vial_plot_canvas)
        self.tabsPlots.addTab(self.vial_plot_tab, "Vial Cycles")

        # Tab 2: Run Calibration Curve
        self.calib_plot_tab = QWidget()
        calib_layout = QVBoxLayout(self.calib_plot_tab)
        self.calib_plot_canvas = FigureCanvas(Figure())
        calib_layout.addWidget(self.calib_plot_canvas)
        self.tabsPlots.addTab(self.calib_plot_tab, "Calibration Curve")
        
        nl = QHBoxLayout()
        self.btnPrev = QPushButton('◀ Prev'); self.btnPrev.clicked.connect(self._prev_vial)        
        self.btnNext = QPushButton('Next ▶');  self.btnNext.clicked.connect(self._next_vial)
        self.lblVial = QLabel('Vial: -'); self.lblVial.setStyleSheet('font-weight:bold; font-size:14px;')
        self.lblVialPlot = QLabel('Plot Field: ')                
        nl.addWidget(self.btnPrev); nl.addStretch(); nl.addWidget(self.lblVial);                  
        nl.addStretch(); nl.addWidget(self.btnNext)
        self.cmbCPMForPlot = QComboBox()
        nl.addStretch();nl.addWidget(self.lblVialPlot); nl.addWidget(self.cmbCPMForPlot)        
        self.cmbCPMForPlot.setMinimumWidth(150)
        self.cmbCPMForPlot.currentTextChanged.connect(self._on_cpm_selection_changed)
        rl.addLayout(nl)
                
        # In _init_ui, right panel:
        self.tabsPreview = QTabWidget()
        self.tblVial = QTableView(); self.modelVial = QStandardItemModel(); self.tblVial.setModel(self.modelVial)
        self.tblVial.setAlternatingRowColors(True)
        self.tblVial.setSelectionBehavior(QTableView.SelectRows)
        self.tblVial.setEditTriggers(QTableView.DoubleClicked | QTableView.SelectedClicked)
        self.tabsPreview.addTab(self.tblVial, "Cycles")
        self.lblVialHint = QLabel("Outliers shown in red. Table lists full cycle details.")
        self.lblVialHint.setStyleSheet('color:#555;')
        rl.addWidget(self.lblVialHint)

        self.calib_plot_canvas.mpl_connect('motion_notify_event', self._on_calib_plot_hover)
        
        # --- Preview results table (per Position) ---
        self.tblPreview = QTableView()
        self.modPreview = QStandardItemModel()
        self.tblPreview.setModel(self.modPreview)
        self.tblPreview.setAlternatingRowColors(True)
        self.tblPreview.setSelectionBehavior(QTableView.SelectRows)
        self.tabsPreview.addTab(self.tblPreview, "Preview Results")
        rl.addWidget(self.tabsPreview)

        self.txtQCReport = QTextEdit()
        self.txtQCReport.setReadOnly(True)
        self.tabsPreview.addTab(self.txtQCReport, "QC Report")
        
        splitter.addWidget(right_w); splitter.setStretchFactor(1, 2)
        
        # Add splitter to content layout (which has collapse button)
        content_layout.addWidget(splitter)
        
        # Add content layout to main layout
        main_layout.addLayout(content_layout)
        
        # Track sidebar state
        self.sidebar_collapsed = False

    def _toggle_sidebar(self):
        """Toggle the left panel visibility for smaller screens"""
        if self.sidebar_collapsed:
            # Expand
            self.left_panel.show()
            self.btnToggleSidebar.setText('◀')
            self.btnToggleSidebar.setToolTip('Collapse Controls Panel')
            self.sidebar_collapsed = False
        else:
            # Collapse
            self.left_panel.hide()
            self.btnToggleSidebar.setText('▶')
            self.btnToggleSidebar.setToolTip('Expand Controls Panel')
            self.sidebar_collapsed = True

    def _is_hidex_matrix(self):
        fmt_id = int(self.cmbFormat.currentData() or 0)
        return fmt_id == 6

    def _is_hidex_list(self):
        fmt_id = int(self.cmbFormat.currentData() or 0)
        return fmt_id == 12

    def _select_protocol(self):
        """Select and apply a protocol"""
        # Extract headers if file loaded
        headers = None
        if hasattr(self, 'file_headers') and self.file_headers:
            headers = self.file_headers
        dlg = ProtocolGUI(
            parent=self,
            module='LSC',
            file_headers=headers,
            restrict_to_module=True
        )
        
        if dlg.exec_() == QDialog.Accepted:
            if hasattr(dlg, 'selected_protocol') and dlg.selected_protocol:
                self._apply_protocol(dlg.selected_protocol)
                logging.info(f"User selected protocol: {dlg.selected_protocol.name}")
            else:
                logging.info("No protocol selected")

    def _manage_protocols(self):
        """Open protocol management dialog"""
        # Extract headers if file loaded
        headers = None
        protocol = self._get_current_protocol()
        current_id = None
        if protocol:
            current_id =protocol.id
        
        if hasattr(self, 'file_headers') and self.file_headers:
            headers = self.file_headers
        dlg = ProtocolGUI(
            parent=self,
            module='LSC',
            current_protocol_id=current_id,
            file_headers=headers,
            restrict_to_module=True
        )
        
        if dlg.exec_() == QDialog.Accepted:
            if hasattr(dlg, 'selected_protocol') and dlg.selected_protocol:
                self._apply_protocol(dlg.selected_protocol)
                logging.info(f"User selected protocol: {dlg.selected_protocol.name}")

    def _apply_protocol(self, protocol: Protocol):
        """Apply a protocol to the current UI"""
        try:
            # mapping_dict = ProtocolManager.get_mapping_dict(protocol)
            mapping_dict = {}
            for mapping in protocol.mappings:
                if mapping.target_field and mapping.source_header:
                    mapping_dict[mapping.target_field] = mapping.source_header
                    mapping_dict[f'{mapping.target_field}_is_net'] = mapping.is_net
                    if hasattr(mapping, 'uncertainty_column') and mapping.uncertainty_column:
                        mapping_dict[f'{mapping.target_field}_unc'] = mapping.uncertainty_column     
                               
            format_id = int(self.cmbFormat.currentData() or 0)
            # Apply mappings to UI dropdowns
            for mapping in protocol.mappings:
                if mapping.target_field == 'CPM' and mapping.source_header:
                    idx = self.cmbWinCPM.findText(mapping.source_header)
                    if idx >= 0:
                        self.cmbWinCPM.setCurrentIndex(idx)
                elif mapping.target_field == 'QIP' and mapping.source_header:
                    idx = self.cmbWinQIP.findText(mapping.source_header)
                    if idx >= 0:
                        self.cmbWinQIP.setCurrentIndex(idx)
                elif mapping.target_field == 'DPM' and mapping.source_header:
                    idx = self.cmbWinDPM.findText(mapping.source_header)
                    if idx >= 0:
                        self.cmbWinDPM.setCurrentIndex(idx)
                elif mapping.target_field == 'Full' and mapping.source_header:
                    idx = self.cmbWinFull.findText(mapping.source_header)
                    if idx >= 0:
                        self.cmbWinFull.setCurrentIndex(idx)
                
                # Apply NET checkboxes
                if mapping.target_field == 'CPM' and hasattr(self, 'chkNetCPM'):
                    self.chkNetCPM.setChecked(mapping.is_net)
                elif mapping.target_field == 'DPM' and hasattr(self, 'chkNetDPM'):
                    self.chkNetDPM.setChecked(mapping.is_net)
            
            # Update calculation settings
            s = protocol.settings
            
            # Signal metric (new key: signal_metric; old key: metric)
            sig = s.get('signal_metric') or s.get('metric', 'CPM')
            idx = self.cmbSignalMetric.findText(sig)
            if idx >= 0:
                self.cmbSignalMetric.setCurrentIndex(idx)

            idx = self.cmbBgdSource.findText(s.get('background_mode', 'Calculated'))
            if idx >= 0:
                self.cmbBgdSource.setCurrentIndex(idx)

            # Efficiency source (new key: efficiency_source; old key: efficiency_mode)
            eff = s.get('efficiency_source') or s.get('efficiency_mode', 'Per-run (computed)')
            idx = self.cmbEffSource.findText(eff)
            if idx >= 0:
                self.cmbEffSource.setCurrentIndex(idx)

            # Outlier settings (new key: outlier_threshold; old key: outlier_sigma)
            outlier_method = s.get('outlier_method', 'Chauvenet')
            idx = self.cmbOutlier.findText(outlier_method)
            if idx >= 0:
                self.cmbOutlier.setCurrentIndex(idx)

            outlier_threshold = s.get('outlier_threshold') or s.get('outlier_sigma', 2.0)
            self.spnThreshold.setValue(float(outlier_threshold))
            
            # Activity unit — stored as int (canonical) or string (legacy "TU" etc.)
            activity_unit = s.get('activity_unit', 1)
            try:
                idx = self.cmbActivityUnit.findData(int(activity_unit))
            except (TypeError, ValueError):
                idx = -1
            if idx < 0:
                idx = self.cmbActivityUnit.findText(str(activity_unit))
            if idx >= 0:
                self.cmbActivityUnit.setCurrentIndex(idx)

            # EF method — stored as int (canonical) or string (legacy "1 --- Deuterium" etc.)
            ef_method = s.get('enrichment_factor_method', 1)
            try:
                idx = self.cmbEFMethod.findData(int(ef_method))
            except (TypeError, ValueError):
                idx = -1
            if idx < 0:
                idx = self.cmbEFMethod.findText(str(ef_method))
            if idx >= 0:
                self.cmbEFMethod.setCurrentIndex(idx)
            
            # Store protocol reference
            self.current_protocol = protocol
            self.lblCurrentProtocol.setText(f"Protocol: {protocol.name}")
            self.lblStatus.setText(f"OK Applied protocol: {protocol.name}")
            logging.info(f"Applied protocol: {protocol.name}")
            
        except Exception as e:
            logging.error(f"Failed to apply protocol: {e}", exc_info=True)
            QMessageBox.warning(self, "Protocol Error", f"Failed to apply protocol: {e}")

    def _get_current_protocol(self) -> Protocol:
        """
        Get the currently active protocol (from UI or default).
        
        REFACTORED: Returns unified Protocol object instead of LSCProtocol
        """
        # Check if user has loaded a protocol
        if hasattr(self, 'current_protocol') and self.current_protocol:
            return self.current_protocol
        
        # Try to load default LSC protocol
        protocol = ProtocolManager.get_default_protocol(module='LSC')
        
        if protocol:
            logging.info(f"Using default LSC protocol: {protocol.name}")
            return protocol
        
        # Build protocol from current UI state (backward compatibility)
        logging.info("No protocol found, building from UI state")
        return self._build_protocol_from_ui()


    def _build_protocol_from_ui(self) -> Protocol:
        """
        Create a protocol object from current UI settings (for backward compatibility).
        
        REFACTORED: Creates unified Protocol with format_id from cmbFormat
        """
        from datetime import datetime
        
        # Get current format ID from combo
        format_id = None
        if hasattr(self, 'cmbFormat'):
            format_id = self.cmbFormat.currentData()
            if format_id is not None:
                format_id = int(format_id)
        
        if not format_id:
            logging.warning("No format selected when building protocol - "
                        "protocol won't be queryable by format")
        
        # Create protocol with unified structure
        protocol = Protocol(
            name=f"Ad-hoc {datetime.now().strftime('%Y%m%d_%H%M')}",
            module='LSC',
            format_id=format_id,  # ← CRITICAL: Added format_id!
            description="Created from UI settings"
        )
        
        # Build mappings from current UI
        protocol.mappings = self._get_current_mappings()
        
        # Build settings dict (JSON-compatible, NO format_id here)
        protocol.settings = self._get_current_settings_dict()
        
        logging.info(f"Built protocol from UI: format_id={format_id}")
        return protocol


    def _get_current_mappings(self) -> list:
        """
        Extract current column mappings from UI.
        
        REFACTORED: Uses unified ColumnMapping structure
        """
        mappings = []
        
        # Get current mapping dict from UI
        format_id = int(self.cmbFormat.currentData() or 0)
        isotope_id = self._get_run_isotope_id()
        mapping_dict = self._get_mapping(format_id, isotope_id)
        
        # Convert to ColumnMapping objects
        order = 0
        for target_field, source_header in mapping_dict.items():
            if target_field.endswith('_is_net') or target_field.endswith('_unc'):
                continue
            
            is_net = mapping_dict.get(f'{target_field}_is_net', False)
            unc_col = mapping_dict.get(f'{target_field}_unc', None)
            
            mappings.append(ColumnMapping(
                target_field=target_field,
                source_header=source_header,
                is_net=bool(is_net),
                requires_background=not bool(is_net),
                uncertainty_column=unc_col,
                display_order=order
            ))
            order += 1
        
        return mappings


    def _get_current_settings_dict(self) -> dict:
        """
        Extract current calculation settings from UI as JSON-compatible dict.
        
        REFACTORED: Returns dict for SettingsJSON instead of ProtocolSettings object
        """
        return {
            "signal_metric": self.cmbSignalMetric.currentText() or 'CPM',
            "efficiency_source": self.cmbEffSource.currentText() or 'Per-run (computed)',
            "outlier_method": self.cmbOutlier.currentText() or 'Chauvenet',
            "outlier_threshold": float(self.spnThreshold.value()),
            "outlier_iterations": 3,
            "outlier_apply_to": 'CPM',
            "activity_unit": self.cmbActivityUnit.currentText() or 'TU',
            "enrichment_factor_method": self.cmbEFMethod.currentText() or 'Spike',
            "background_mode": 'Computed',
            "min_count_time": 30.0,
            "max_rsd": 5.0,
            "require_qip_check": True
        }

    def _compute_background_from_file(self, protocol: Protocol):
        """Compute background from file columns using protocol mappings"""
        try:
            # Find background mapping
            bkg_mapping = next((m for m in protocol.mappings if m.target_field == 'Background'), None)
            
            if bkg_mapping and bkg_mapping.source_header in self.raw_data.columns:
                # Use background values from file
                bkg_values = pd.to_numeric(self.raw_data[bkg_mapping.source_header], errors='coerce').dropna()
                if len(bkg_values) > 0:
                    self.bkg_mean = float(bkg_values.mean())
                    self.bkg_unc = float(bkg_values.std(ddof=1)) if len(bkg_values) >= 2 else 0.0
                    logging.info(f"Background from file: {self.bkg_mean:.3f} ± {self.bkg_unc:.3f}")
                else:
                    logging.warning("Background column found but no valid values")
                    self.bkg_mean = 0.0
                    self.bkg_unc = 0.0
            else:
                logging.warning(f"Background column '{bkg_mapping.source_header if bkg_mapping else 'N/A'}' not found in file")
                self.bkg_mean = 0.0
                self.bkg_unc = 0.0
        except Exception as e:
            logging.error(f"Failed to compute background from file: {e}")
            self.bkg_mean = 0.0
            self.bkg_unc = 0.0

    def _compute_background_from_samples(self):
        """Compute background from blank samples (existing logic)"""
        # This calls the existing compute_run_params which already does this
        if hasattr(self, 'computed_means') and self.computed_means is not None:
            self.bkg_mean, self.bkg_unc, self.eff, self.eff_unc = compute_run_params(
                self.run_id, self.computed_means
            )
            # logging.info(f"Background from samples: {self.bkg_mean:.3f} ± {self.bkg_unc:.3f}")
    
    def _update_eff_pct_toggle_visibility(self):
        fmt_id = int(self.cmbFormat.currentData() or 0)
        fmt_name = (self.cmbFormat.currentText() or "").lower()
        is_hidex_me = (fmt_id == 6) or ("hidex matrix" in fmt_name)
        # self.chkUseEffPct.setVisible(is_hidex_me)

    def _get_calc_settings(self, fmt_id, isotope_id):
        """
        Load calculation settings for the given format and isotope.
        Returns a dict with keys: 'activity_unit', 'ef_method', 'signal_metric', 'eff_source', 'outlier_method', 'outlier_param'
        """
        # Example: store in a new table, or in GUItblImportMapping with special TargetField keys
        mapping = self._get_saved_mapping(fmt_id, isotope_id)
        return {
            'activity_unit': mapping.get('activity_unit', 1),
            'ef_method': mapping.get('ef_method', 2),
            'signal_metric': mapping.get('signal_metric', 'CPM'),
            'eff_source': mapping.get('eff_source', 'Per-run (computed)'),
            'outlier_method': mapping.get('outlier_method', 'None'),
            'outlier_param': mapping.get('outlier_param', 2.0),
        }

    def _set_calc_settings(self, fmt_id, isotope_id):
        """
        Restore calculation settings to the UI for the given format and isotope.
        """
        s = self._get_calc_settings(fmt_id, isotope_id)
        # Set UI controls
        idx = self.cmbActivityUnit.findData(int(s['activity_unit']))
        if idx >= 0: self.cmbActivityUnit.setCurrentIndex(idx)
        idx = self.cmbEFMethod.findData(int(s['ef_method']))
        if idx >= 0: self.cmbEFMethod.setCurrentIndex(idx)
        idx = self.cmbSignalMetric.findText(s['signal_metric'])
        if idx >= 0: self.cmbSignalMetric.setCurrentIndex(idx)
        idx = self.cmbEffSource.findText(s['eff_source'])
        if idx >= 0: self.cmbEffSource.setCurrentIndex(idx)
        idx = self.cmbOutlier.findText(s['outlier_method'])
        if idx >= 0: self.cmbOutlier.setCurrentIndex(idx)
        self.spnThreshold.setValue(float(s['outlier_param']))

    def _save_calc_settings(self, fmt_id, isotope_id):
        mapping = self._get_saved_mapping(fmt_id, isotope_id)
        mapping['activity_unit'] = int(self.cmbActivityUnit.currentData() or 1)
        mapping['ef_method'] = int(self.cmbEFMethod.currentData() or 2)
        mapping['signal_metric'] = self.cmbSignalMetric.currentText() or "CPM"
        mapping['eff_source'] = self.cmbEffSource.currentText() or "Per-run (computed)"
        mapping['outlier_method'] = self.cmbOutlier.currentText() or "None"
        mapping['outlier_param'] = float(self.spnThreshold.value())
        self._save_mapping_to_db(fmt_id, isotope_id, mapping)
        
    def _get_mapping(self, format_id: int, isotope_id: int) -> dict:
        """
        Unified getter with backward-compatible fallback for legacy GlobalValue keys.
        Primary source: TRIMS.GUItblImportMapping.
        Fallback (Quantulus only): GlobalValue keys for CPM/QIP windows.
        """
        # Primary: DB table
        mapping = self._get_saved_mapping(format_id, isotope_id)

        # Fallback for Quantulus only (read-only, do NOT write back to GlobalValue)
        if (format_id in (1, 2)) and (not mapping or 'CPM' not in mapping or 'QIP' not in mapping):
            try:
                if self.equipment_id:
                    key_cpm = f"{self.equipment_id}_CPM_OMPTIMIZED_WINDOW"
                    key_qip = f"{self.equipment_id}_CPM_QIP_WINDOW"
                    legacy_cpm = get_global_value(key_cpm)
                    legacy_qip = get_global_value(key_qip)
                    if legacy_cpm and 'CPM' not in mapping:
                        mapping['CPM'] = _canon_quantulus_source(legacy_cpm, 'CPM') or 'CPM1'
                    if legacy_qip and 'QIP' not in mapping:
                        mapping['QIP'] = 'SQP'  # Quantulus QIP = SQP
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        return mapping

    def _save_mapping(self, format_id: int, isotope_id: int, mapping: dict):
        """Unified saver: always persist to TRIMS.GUItblImportMapping."""
        self._save_mapping_to_db(format_id, isotope_id, mapping)

    def _set_mapping(self, format_id: int, mapping: dict):
        """
        Apply mapping to the UI widgets (combos).
        Works for both Quantulus and non-Quantulus because we now use headers from ParserStrategy.
        """
        # Apply CPM/QIP to the 'Windows' combos if present
        if 'CPM' in mapping and mapping['CPM']:
            i = self.cmbWinCPM.findText(mapping['CPM'])
            if i >= 0: self.cmbWinCPM.setCurrentIndex(i)
        if 'QIP' in mapping and mapping['QIP']:
            i = self.cmbWinQIP.findText(mapping['QIP'])
            if i >= 0: self.cmbWinQIP.setCurrentIndex(i)
        # Optional: DPM/Full if you want to persist those as well
        if 'DPM' in mapping and mapping['DPM']:
            i = self.cmbWinDPM.findText(mapping['DPM'])
            if i >= 0: self.cmbWinDPM.setCurrentIndex(i)
        if 'Full' in mapping and mapping['Full']:
            i = self.cmbWinFull.findText(mapping['Full'])
            if i >= 0: self.cmbWinFull.setCurrentIndex(i)
            
    def _save_mapping_to_db(self, format_id, isotope_id, mapping_dict):
        with db_manager.get_connection() as conn:
            # Build full mapping dict including net-CPM/DPM flags
            full_mapping = {k: v for k, v in (mapping_dict or {}).items()
                            if v not in (None, "", "-- Select Source --")}
            if hasattr(self, 'chkNetCPM') and self.chkNetCPM.isChecked():
                full_mapping['CPM_is_net'] = 'True'
            if hasattr(self, 'chkNetDPM') and self.chkNetDPM.isChecked():
                full_mapping['DPM_is_net'] = 'True'

            params = [
                {'fid': format_id, 'isoid': isotope_id, 'target': target, 'source': str(source)}
                for target, source in full_mapping.items()
            ]
            if params:
                conn.execute(text("""
                    INSERT INTO TRIMS.GUItblImportMapping
                        (FormatID, IsotopeID, TargetField, SourceHeader)
                    VALUES (:fid, :isoid, :target, :source)
                    ON CONFLICT (FormatID, IsotopeID, TargetField)
                    DO UPDATE SET SourceHeader = EXCLUDED.SourceHeader
                """), params)
            conn.commit()

    def _get_saved_mapping(self, format_id, isotope_id):
        """
        Returns a dict of all TargetField → SourceHeader mappings for the given format and isotope.
        This includes extended keys like 'CPM_is_net', 'CPM_unc', etc.
        """
        mapping = {}
        with db_manager.get_connection() as conn:
            rows = conn.execute(text("""
                SELECT TargetField, SourceHeader
                FROM TRIMS.GUItblImportMapping
                WHERE FormatID = :fid AND IsotopeID = :isoid
            """), {'fid': format_id, 'isoid': isotope_id}).fetchall()
            for r in rows:
                raw = (r.SourceHeader or '').strip()
                mapping[r.TargetField] = raw
        return mapping

    # 2) When restoring to combos
    def _set_saved_mapping(self, fmt_id: int, saved: dict):
        if fmt_id in (1, 2):  # Quantulus
            if 'CPM' in saved and saved['CPM']:
                key = self._normalize_quantulus_sourceheader(saved['CPM'])
                if key:
                    i = self.cmbWinCPM.findData(key)
                    if i >= 0:
                        self.cmbWinCPM.setCurrentIndex(i)
            if 'QIP' in saved and saved['QIP']:
                key = self._normalize_quantulus_sourceheader(saved['QIP'])
                if key:
                    i = self.cmbWinQIP.findData(key)
                    if i >= 0:
                        self.cmbWinQIP.setCurrentIndex(i)

    def _set_windows_net_checkboxes(self, mapping: dict):
        """
        Set the 'Net?' checkboxes in the Windows Settings group
        based on the mapping dict (keys like 'CPM_is_net', etc.).
        """
        # Example: self.chkNetCPM, self.chkNetDPM, self.chkNetFull, self.chkNetQIP
        if hasattr(self, 'chkNetCPM') and 'CPM_is_net' in mapping:
            self.chkNetCPM.setChecked(str(mapping['CPM_is_net']).strip() in ("1", "true", "True", "yes", "y"))
        if hasattr(self, 'chkNetDPM') and 'DPM_is_net' in mapping:
            self.chkNetDPM.setChecked(str(mapping['DPM_is_net']).strip() in ("1", "true", "True", "yes", "y"))
        if hasattr(self, 'chkNetFull') and 'Full_is_net' in mapping:
            self.chkNetFull.setChecked(str(mapping['Full_is_net']).strip() in ("1", "true", "True", "yes", "y"))
        if hasattr(self, 'chkNetQIP') and 'QIP_is_net' in mapping:
            self.chkNetQIP.setChecked(str(mapping['QIP_is_net']).strip() in ("1", "true", "True", "yes", "y"))
                         
    def _populate_quantulus_combos_from_lines(self, lines):
        """
        Populate all Quantulus window combos (CPM, DPM, Full, QIP) with:
        - __None__ as the default
        - CPM1..CPM8 (labelled with ranges if available)
        """
        wins = parse_windows(lines)
        win_by_no = {}
        for w in wins:
            try:
                n = int(getattr(w, 'wNumber', 0))
                win_by_non = f"[{int(w.channelFrom)}–{int(w.channelTo)}]"
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

        combos = [self.cmbWinCPM, self.cmbWinDPM, self.cmbWinFull, self.cmbWinQIP]
        for cb in combos:
            cb.clear()
            cb.addItem("__None__", None)
            for n in range(1, 9):
                token = f"CPM{n}"
                rng   = win_by_no.get(n)
                label = f"{token}: {rng}" if rng else token
                cb.addItem(label, token)

        # For QIP, add SQP as a special option (in addition to CPMs)
        self.cmbWinQIP.addItem("SQP (QIP)", "SQP")
               
    def _populate_calc_combos(self):
        """Populate Unit and Method selections."""
        with db_manager.get_connection() as conn:
            units = conn.execute(text("SELECT UnitID, ShortName FROM MeasurementUnit WHERE UnitID < 5 ORDER BY UnitID")).fetchall()
            for u in units: self.cmbActivityUnit.addItem(u.ShortName, u.UnitID)
            self.cmbActivityUnit.setCurrentIndex(self.cmbActivityUnit.findData(1)) # TU

            _sep = "' --- '"
            _concat = db_manager.sql_concat('ID', _sep, 'sName')
            methods = conn.execute(text(f"SELECT ID, {_concat} FROM GuitblEnrichmentFactorMethod WHERE ID IN (0,1,2,4) ORDER BY ID")).fetchall()
            for m in methods: self.cmbEFMethod.addItem(m[1], m[0])
            self.cmbEFMethod.setCurrentIndex(self.cmbEFMethod.findData(2)) # Spike

    def _resolve_cpm_column_for_display(self, df: pd.DataFrame) -> str:
        """
        Returns the best CPM-like column for plotting and vial table highlighting.
        Priority:
        1) NetCPM_fit
        2) "CPM H-3 (cpm fit)" (HIDEX ME native header)
        3) MeanH3
        4) CPM  (canonical column your parser fills for HIDEX ME)
        5) CPMn/CPMroiN/CPM1..CPM8 (Quantulus/HIDEX List)
        """
        cols = list(df.columns)

        # 1–4: HIDEX ME / canonical
        if 'NetCPM_fit' in cols: return 'NetCPM_fit'
        if 'CPM H-3 (cpm fit)' in cols: return 'CPM H-3 (cpm fit)'
        if 'MeanH3' in cols: return 'MeanH3'
        if 'CPM' in cols: return 'CPM'

        # 5: legacy windowed names (Quantulus/Hidex List exports)
        # CPM1..CPM8
        q_cols = [c for c in cols if re.fullmatch(r'CPM\d+', c)]
        if q_cols:
            q_cols.sort(key=lambda x: int(re.findall(r'\d+', x)[0]))
            return q_cols[0]

        # CPMroiN
        roi_cols = [c for c in cols if re.fullmatch(r'CPMroi\d+', c, flags=re.I)]
        if roi_cols:
            roi_cols.sort(key=lambda x: int(re.findall(r'\d+', x)[0]))
            return roi_cols[0]

        # If nothing matches, return last resort
        return cols[-1] if cols else 'CPM'
        
    def _populate_cpm_combo(self, df):
        """
        Populate "Plot Field" combo with ACTUAL MEASUREMENT COLUMNS from raw data.
        
        This shows the SOURCE HEADERS (from file), not target headers (CPM, DPM, etc.)
        User can plot any numeric measurement column from the imported file.
        """
        if df is None or df.empty:
            return
        
        cols = list(df.columns)
        
        # Get protocol's preferred CPM column (if any)
        protocol_cpm_header = None
        try:
            protocol = self._get_current_protocol()
            cpm_mapping = next((m for m in protocol.mappings if m.target_field == 'CPM'), None)
            if cpm_mapping and cpm_mapping.source_header:
                protocol_cpm_header = cpm_mapping.source_header
        except:
            pass
        
        self.cmbCPMForPlot.blockSignals(True)
        self.cmbCPMForPlot.clear()
        
        # =========================================================================
        # STRATEGY: Add ALL numeric columns from raw data, in smart order
        # =========================================================================
        
        # Add protocol CPM first (if specified and exists)
        if protocol_cpm_header and protocol_cpm_header in cols:
            try:
                if pd.api.types.is_numeric_dtype(df[protocol_cpm_header]):
                    self.cmbCPMForPlot.addItem(protocol_cpm_header, protocol_cpm_header)
            except:
                pass
        
        # Priority measurement columns (common names across formats)
        priority_names = [
            # CPM variants
            'CPM', 'NetCPM', 'GrossCPM', 'MeanCPM', 'NetCPM_fit',
            # Quantulus windows
            'CPM1', 'CPM2', 'CPM3', 'CPM4', 'CPM5',
            # HIDEX variants
            'CPM H-3 (cpm fit)', 'CPM C-14 (cpm fit)', 'Mean H-3', 'MeanH3',
            'CPMroi1', 'CPMroi2', 'CPMroi3',
            # DPM variants
            'DPM', 'NetDPM', 'GrossDPM', 'MeanDPM',
            # Quench parameters
            'QIP', 'QPI', 'SQP', 'QPE', 'tSIE',
            # Other measurements
            'TDCR', 'SIS', 'Efficiency', 'Eff', 'Eff %',
        ]
        
        # Add priority columns in order (skip if already added via protocol)
        for col_name in priority_names:
            if col_name in cols and col_name != protocol_cpm_header:
                try:
                    if pd.api.types.is_numeric_dtype(df[col_name]):
                        self.cmbCPMForPlot.addItem(col_name, col_name)
                except:
                    pass
        
        # Add any remaining numeric columns not yet added
        for col in cols:
            # Skip if already in combo
            if self.cmbCPMForPlot.findData(col) >= 0:
                continue
            
            # Skip non-measurement columns
            if col in ['Position', 'VialPos', 'Cycle', 'Repeat', 'CountTime', 
                    'SampleType', 'IsOutlier', 'start_datetime', 'stime', 'meas_file']:
                continue
            
            # Add if numeric
            try:
                if pd.api.types.is_numeric_dtype(df[col]):
                    self.cmbCPMForPlot.addItem(col, col)
            except:
                pass
        
        self.cmbCPMForPlot.blockSignals(False)
        
        # Select first item (protocol CPM if added, otherwise first available)
        if self.cmbCPMForPlot.count() > 0:
            self.cmbCPMForPlot.setCurrentIndex(0)
            selected_col = self.cmbCPMForPlot.itemData(0) or self.cmbCPMForPlot.itemText(0)
            self.plot_cpm_col = selected_col
            self._on_cpm_selection_changed(self.cmbCPMForPlot.itemText(0))
        
        logging.info(f"Populated plot field combo with {self.cmbCPMForPlot.count()} measurement columns")

    def _on_cpm_selection_changed(self, display_text: str):
        """
        When user picks a CPM, resolve to real column name and refresh the plot.
        """
        # Prefer the stored UserData (real column name)
        data = None
        i = self.cmbCPMForPlot.currentIndex() if hasattr(self, 'cmbCPMForPlot') else -1
        if i >= 0:
            data = self.cmbCPMForPlot.itemData(i)
        self.plot_cpm_col = data or display_text or 'CPM'
        self._update_plot()
 
    def _on_cpm_window_changed(self, idx: int):
        tok = self.cmbWinCPM.itemData(idx)  # 'CPM#' or None
        if not tok or not isinstance(tok, str):
            return
        # Update mapping in memory
        if hasattr(self, 'worker') and hasattr(self.worker, 'settings'):
            self.worker.settings.setdefault('mapping', {})['CPM'] = tok
            if tok.startswith('CPM') and tok[3:].isdigit():
                self.worker.settings['mapping']['CPM_window'] = int(tok[3:])
        # Apply selection to the entire DataFrame so Step‑2 sees a consistent CPM
        if isinstance(self.raw_data, pd.DataFrame) and tok in self.raw_data.columns:
            self.raw_data['CPM'] = pd.to_numeric(self.raw_datatok, errors='coerce')
            self._refresh_vial_views()
            if getattr(self, 'computed_means', None) is not None:
                self._on_compute_clicked()

                
    def _load_formats(self):

        self._populate_file_formats()
        self._auto_select_format_from_procedure()
        
        # Load default protocol for initially selected format
        self._load_initial_protocol()

    def _populate_file_formats(self):
        """Populate file formats for LSC - SIMPLE VERSION"""
        try:
            # Block signals to prevent _on_format_changed firing during population
            with QSignalBlocker(self.cmbFormat):
                self.cmbFormat.clear()
                
                formats = ProtocolManager.get_file_formats_for_module('LSC')
                
                if not formats:
                    self.cmbFormat.addItem("(No formats available)", None)
                    self.cmbFormat.setEnabled(False)
                    logging.warning("No LSC file formats found in database")
                    return
                
                # Add formats
                for fmt in formats:
                    display = fmt['name']
                    if fmt['instrument_name']:
                        display += f" ({fmt['instrument_name']}"
                        if fmt['instrument_model']:
                            display += f" {fmt['instrument_model']}"
                        display += ")"
                    
                    self.cmbFormat.addItem(display, fmt['id'])
                
                self.cmbFormat.setEnabled(True)
                logging.info(f"Loaded {len(formats)} LSC file formats")
                
        except Exception as e:
            logging.error(f"Failed to load file formats: {e}", exc_info=True)
            self.cmbFormat.addItem("(Database error)", None)
            self.cmbFormat.setEnabled(False)

    def _auto_select_format_from_procedure(self):
        """
        Dynamically selects the import format based on the active run's procedure configuration (AnalysisImportFormat).
        Falls back to _auto_select_format_by_name() if not configured or not found.
        """
        analysis_import_format = None
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT ap.AnalysisImportFormat
                    FROM TRIMS.LSCRun r
                    JOIN AnalysisProcedure ap ON ap.ProcedureID = r.ProcedureID
                    WHERE r.RunID = :rid
                """), {'rid': self.run_id}).fetchone()
                if row and row[0] is not None:
                    analysis_import_format = int(row[0])
        except Exception as e:
            logging.error(f"Failed to query AnalysisImportFormat for run {self.run_id}: {e}", exc_info=True)

        if analysis_import_format is not None:
            idx = self.cmbFormat.findData(analysis_import_format)
            if idx >= 0:
                self.cmbFormat.setCurrentIndex(idx)
                self.lblStatus.setText(f"Auto-selected format: {self.cmbFormat.itemText(idx)}")
                logging.info(f"Auto-selected format by procedure: format_id={analysis_import_format}")
                return
            else:
                logging.warning(f"Format ID {analysis_import_format} from procedure not found in combo box, falling back to name auto-select")

        # Fallback
        self._auto_select_format_by_name()
                
    def _auto_select_format_by_name(self):
        """Dynamically selects the import format based on Equipment Model name."""
        if not self.model_name:
            return

        target_keyword = ""
        if 'hidex list' in self.model_name.lower():
            target_keyword = "Hidex List"
        elif 'quantulus' in self.model_name.lower():
            target_keyword = "Quantulus Registry"

        if target_keyword:
            # Iterate through the combo box items
            for i in range(self.cmbFormat.count()):
                display_text = self.cmbFormat.itemText(i)
                if target_keyword.lower() in display_text.lower():
                    self.cmbFormat.setCurrentIndex(i)
                    self.lblStatus.setText(f"Auto-selected format: {display_text}")
                    break
    
    def _load_initial_protocol(self):
        """
        Load default protocol for LSC module when dialog opens.
        Called after _load_formats() completes.
        
        REFACTORED: Uses unified ProtocolManager with format-specific filtering
        """
        try:
            format_id = None
            if hasattr(self, 'cmbFormat'):
                format_id = self.cmbFormat.currentData()
                if format_id is not None:
                    format_id = int(format_id)

            protocol = None
            if format_id:
                protocol = ProtocolManager.get_default_protocol(module='LSC', file_format_id=format_id)
            
            if not protocol:
                protocol = ProtocolManager.get_default_protocol(module='LSC')

            if protocol:
                self._apply_protocol(protocol)
                self.lblCurrentProtocol.setText(f"Protocol: {protocol.name} (Default)")
                logging.info(f"Initial load: Applied default LSC protocol {protocol.name} for format {format_id}")
                
                # Apply calculation settings
                self._apply_protocol_calc_settings(protocol.settings)
                
                # Apply mappings
                if format_id:
                    self._apply_format_specific_mappings(protocol, format_id)
            else:
                logging.info("No default LSC protocol found")
        except Exception as e:
            logging.warning(f"Could not load initial protocol: {e}")
                
    def _add_none_option(self, combo):
        combo.clear(); combo.addItem('_None_', None)

    def _load_defaults_from_global(self):        
        # NOTE: These are legacy defaults for Quantulus only (read-only).
        # Persisted per-format mappings now live in TRIMS.GUItblImportMapping

        self._add_none_option(self.cmbWinCPM)
        self._add_none_option(self.cmbWinDPM)
        self._add_none_option(self.cmbWinFull)
        self._add_none_option(self.cmbWinQIP)

        if not self.equipment_id:
            return

        key_cpm  = f"{self.equipment_id}_CPM_OMPTIMIZED_WINDOW"
        key_full = f"{self.equipment_id}_CPM_OPEN_WINDOW"
        key_qip  = f"{self.equipment_id}_CPM_QIP_WINDOW"
        try:
            v_cpm  = get_global_value(key_cpm)
            v_full = get_global_value(key_full)
            v_qip  = get_global_value(key_qip)
            if v_cpm is not None:
                i = self.cmbWinCPM.findData(_canon_quantulus_source(v_cpm, 'CPM'))
                if i >= 0: self.cmbWinCPM.setCurrentIndex(i)
            if v_full is not None:
                i = self.cmbWinFull.findData(_canon_quantulus_source(v_full, 'CPM'))
                if i >= 0: self.cmbWinFull.setCurrentIndex(i)
            if v_qip is not None:
                i = self.cmbWinQIP.findData('SQP')
                if i >= 0: self.cmbWinQIP.setCurrentIndex(i)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    def _load_outlier_prefs(self):
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text('SELECT OutlierMethod, OutlierSigma FROM TRIMS.LSCRun WHERE RunID=:rid'),
                                   {'rid': self.run_id}).fetchone()
                if row:
                    if row.OutlierMethod:
                        idx = self.cmbOutlier.findText(str(row.OutlierMethod))
                        if idx >= 0: self.cmbOutlier.setCurrentIndex(idx)
                    if row.OutlierSigma is not None:
                        self.spnThreshold.setValue(float(row.OutlierSigma))
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        if self.equipment_id:
            val = get_global_value(f"{self.equipment_id}_PlotSigma", default=None)
            if val is not None:
                try: self.spnBandSigma.setValue(float(val))
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Select File', '', 'Text/CSV (*.txt *.dat *.csv);;All (*.*)')
        if path:
            # RESET UI when new file selected
            self._reset_ui_to_defaults()
            
            self.txtPath.setText(path)
            self._load_preview(path)           

    def _load_preview(self, path):
        fmt_id = int(self.cmbFormat.currentData() or 0)
        fmt_name = self.cmbFormat.currentText()        
        
        parser = make_lsc_parser(fmt_id, fmt_name, path)    
        src_headers = parser.get_headers()   
        self.file_headers = src_headers 
        if fmt_id in (1, 2) or "quantulus" in (fmt_name or "").lower():
            try:
                with open(path, 'r', encoding='latin1', errors='ignore') as f:
                    lines = f.read().splitlines()
                # Build combos as CPM1/2... & SQP with range labels
                self._populate_quantulus_combos_from_lines(lines)
                isotope_id = self._get_run_isotope_id()
                saved = self._get_saved_mapping(fmt_id, isotope_id)
                self._set_saved_mapping(fmt_id, saved)
                self.lblStatus.setText("Quantulus windows detected. Select CPM (and QIP=SQP).")
            except Exception as e:
                self.lblStatus.setText(f"Quantulus Preview Error: {e}")
            return

        else:
            # Delimited: show headers for mapping
            try:
                src_headers = parser.get_headers()
                self.file_headers = src_headers
                print(f"🔍 STORED HEADERS: {len(src_headers)} items")
                isotope_id  = 200
                saved_map   = self._get_saved_mapping(fmt_id, isotope_id)
                self._populate_mapping_ui(src_headers, saved_map)
                self.lblStatus.setText(f"Detected format: {fmt_name}. Review mapping and import.")
            except Exception as e:
                self.lblStatus.setText(f"Preview Error: {e}")


    def _populate_mapping_ui(self, source_headers, saved_map):
        """
        Matches source file headers to TRIMS dropdowns for all non-Quantulus formats.
        For HIDEX Matrix (FormatID=6), if no saved mapping exists, auto-pick
        good defaults based on detected headers.
        """
        # Clear and fill the dropdowns with headers from the actual file
        for cb in [self.cmbWinCPM, self.cmbWinDPM, self.cmbWinFull, self.cmbWinQIP]:
            cb.clear()
            cb.addItem('-- Select Source --', None)
            for header in source_headers:
                cb.addItem(header, header)

        # Apply any saved mapping first (respects existing user choices)
        if 'CPM' in saved_map:
            idx = self.cmbWinCPM.findText(saved_map['CPM'])
            if idx >= 0: self.cmbWinCPM.setCurrentIndex(idx)
        if 'QIP' in saved_map:
            idx = self.cmbWinQIP.findText(saved_map['QIP'])
            if idx >= 0: self.cmbWinQIP.setCurrentIndex(idx)
        if 'DPM' in saved_map:
            idx = self.cmbWinDPM.findText(saved_map['DPM'])
            if idx >= 0: self.cmbWinDPM.setCurrentIndex(idx)
        if 'Full' in saved_map:
            idx = self.cmbWinFull.findText(saved_map['Full'])
            if idx >= 0: self.cmbWinFull.setCurrentIndex(idx)

        # ----- HIDEX Matrix smart defaults (FormatID = 6) -----
        try:
            fmt_id = int(self.cmbFormat.currentData() or 0)
        except Exception:
            fmt_id = 0

        if fmt_id == 6:
            # If CPM not chosen yet, try to auto-pick a sensible CPM column
            if self.cmbWinCPM.currentIndex() <= 0:
                preferred = [
                    "CPM H-3 (cpm fit)",  # MikroWin net fit
                    "Mean H-3",           # Robust fallback if no net
                    "MeanCPM"             # Some reports use this label
                ]
                for name in preferred:
                    j = self.cmbWinCPM.findText(name)
                    if j >= 0:
                        self.cmbWinCPM.setCurrentIndex(j)
                        break

            # If QIP not chosen yet, try QPE then QIP
            if self.cmbWinQIP.currentIndex() <= 0:
                for name in ["QPE", "QIP"]:
                    j = self.cmbWinQIP.findText(name)
                    if j >= 0:
                        self.cmbWinQIP.setCurrentIndex(j)
                        break
            
    def _populate_quantulus_combos(self, wins):
        """Restores Quantulus-specific window mapping."""
        for cb in [self.cmbWinCPM, self.cmbWinDPM, self.cmbWinFull, self.cmbWinQIP]:
            # Preserve the default None option
            cb.clear(); cb.addItem('_None_', None)
            
            for w in wins:
                # Window 9 is typically SQP/QIP only
                if cb == self.cmbWinQIP:
                    cb.addItem(f"{w.wNumber} — {w.label}", w.wNumber)
                elif w.wNumber != 9:
                    cb.addItem(f"{w.wNumber} — {w.label}", w.wNumber)

        # Re-apply defaults from global settings if available
        self._load_defaults_from_global()
        
    def _populate_hidex_combos(self, columns):
        """Populates dropdowns with Hidex CSV column names."""
        for cb in [self.cmbWinCPM, self.cmbWinDPM, self.cmbWinFull, self.cmbWinQIP]:
            cb.clear()
            cb.addItem('_None_', None)
            for col in columns:
                cb.addItem(col, col)

        # Attempt auto-mapping based on your VBA defaults
        idx_cpm = self.cmbWinCPM.findText("CPMroi1")
        if idx_cpm >= 0: self.cmbWinCPM.setCurrentIndex(idx_cpm)
        
        idx_qip = self.cmbWinQIP.findText("QPI")
        if idx_qip >= 0: self.cmbWinQIP.setCurrentIndex(idx_qip)                        

    def _refresh_vial_views(self):
        self._update_plot(); self._update_vial_table()

    def _update_plot(self):
        if self.raw_data is None or self.current_pos is None:
            return
        try:
            subset = self.raw_data[self.raw_data['Position'] == self.current_pos].copy()
            if subset.empty: return
            if 'IsOutlier' not in subset.columns: subset['IsOutlier'] = False                        
            cpm_col = getattr(self, 'plot_cpm_col', None) or 'CPM'
            if cpm_col not in subset.columns:
                # fall back gracefully
                cpm_col = 'CPM' if 'CPM' in subset.columns else subset.columns[-1]
            vals = pd.to_numeric(subset[cpm_col], errors='coerce').values
            indices = np.arange(len(vals)) + 1
            mu = np.nanmean(vals)
            stdv = np.nanstd(vals)
            sigma = float(self.spnBandSigma.value())
            
            fig = self.vial_plot_canvas.figure
            fig.clear()
            ax = fig.add_subplot(111)
            # self.plot_figure.clear(); ax = self.plot_figure.add_subplot(111)
            outlier_mask = subset['IsOutlier'].astype(bool)
            valid_mask = (~outlier_mask) & (~np.isnan(vals))
            if np.any(valid_mask): ax.scatter(indices[valid_mask], vals[valid_mask], c='blue', marker='o', label='Valid')
            if np.any(outlier_mask): ax.scatter(indices[outlier_mask], vals[outlier_mask], c='red', marker='x', label='Outlier')
            ax.axhline(mu, color='green', linestyle='-', linewidth=1.5, label='Mean')
            ax.axhline(mu + sigma*stdv, color='magenta', linestyle='--', linewidth=1.0, label=f'+{sigma}σ')
            ax.axhline(mu - sigma*stdv, color='magenta', linestyle='--', linewidth=1.0, label=f'-{sigma}σ')
            ax.set_title(f"Pos {self.current_pos} ({len(vals)} cycles) μ={mu:.3f}, σ={stdv:.3f}")
            ax.set_xlabel('Cycle'); ax.set_ylabel('CPM'); ax.legend(loc='best')
            self.vial_plot_canvas.draw(); self.lblVial.setText(f'Vial: {self.current_pos}')
        except Exception as e:
            logging.error(f'Plot failed: {e}')

    def _get_available_headers(self):
        """
        Extract headers from already-populated window combos.
        Returns all unique headers including potential uncertainty columns.
        """
        headers = []
        
        # Extract from window combos
        for combo in [self.cmbWinCPM, self.cmbWinDPM, self.cmbWinFull, self.cmbWinQIP]:
            for i in range(combo.count()):
                header = combo.itemText(i)
                data = combo.itemData(i)
                # Skip placeholder items
                if header and data and not header.startswith('--') and not header.startswith('_'):
                    if header not in headers:
                        headers.append(header)
        
        # ENHANCEMENT: Also check raw_data columns for uncertainty columns
        # This ensures we capture Unc.CPM, Unc.DPM, etc. that might not be in window combos
        if hasattr(self, 'raw_data') and self.raw_data is not None:
            for col in self.raw_data.columns:
                col_str = str(col).strip()
                # Add uncertainty-related columns
                if any(x in col_str.lower() for x in ['unc', 'err', 'std', 'sigma', 'uncertainty', 'error']):
                    if col_str not in headers:
                        headers.append(col_str)
        
        return headers
    
    def _update_vial_table(self):
        """
        Render raw cycles for the current vial, for any format.
        - Priority columns always first (Quantulus and others).
        - Selected CPM column highlighted (light yellow).
        - Outlier rows highlighted (light red).
        - CPM column always assigned the correct value.
        """
        if self.raw_data is None or self.current_pos is None:
            return

        subset = self.raw_data[self.raw_data['Position'] == self.current_pos].copy()
        if subset.empty:
            self.modelVial.clear()
            return

        # Priority columns (always first)
        priority = ['Position', 'VialPos', 'Cycle', 'Repeat', 'CountTime', 'CPM', 'QIP', 'IsOutlier']
        all_cols = list(subset.columns)
        ordered_cols = [c for c in priority if c in all_cols] + [c for c in all_cols if c not in priority]

        # Determine selected CPM column for highlighting
        selected_colname = None
        try:
            mapping_dict = getattr(self.worker, 'settings', {}).get('mapping', {}) if hasattr(self, 'worker') else {}
            cpm_col = mapping_dict.get('CPM', None)
            
            if cpm_col and cpm_col in subset.columns:
                selected_colname = cpm_col
            else:
                selected_colname = self._resolve_cpm_column_for_display(subset)
        except Exception:
            selected_colname = None
    
        # Assign CPM for computation if possible
        if selected_colname and 'CPM' in subset.columns and selected_colname in subset.columns:
            subset['CPM'] = pd.to_numeric(subset[selected_colname], errors='coerce')
        elif 'CPM' in subset.columns:
            # CPM already present from parser; keep it
            pass
        else:
            # Ultimate fallback: try to create CPM from any detectable column
            guessed = self._resolve_cpm_column_for_display(subset)
            if guessed in subset.columns:
                subset['CPM'] = pd.to_numeric(subset[guessed], errors='coerce')

        # If we still can't determine a CPM, don't raise—render the table without highlight
        if 'CPM' not in subset.columns:
            logging.warning("No usable CPM column found; table rendered without CPM.")

        # Build table rows
        self.modelVial.clear()
        self.modelVial.setHorizontalHeaderLabels(ordered_cols)

        for idx, row in subset.iterrows():
            items = []
            is_outlier = bool(row.get('IsOutlier', False))
            for col in ordered_cols:
                val = row.get(col)
                # Arrays/lists support
                if isinstance(val, (np.ndarray, list, pd.Series)):
                    try:
                        if pd.isna(val).all():
                            items.append(QStandardItem(''))
                            continue
                    except Exception as e:

                        logging.warning(f"Exception caught: {e}")
                    val_str = ', '.join(
                        (f"{float(v):.3f}" if isinstance(v, (float, np.floating)) and not pd.isna(v) else str(v))
                        for v in val
                    )
                    items.append(QStandardItem(val_str))
                    continue
                # NaN to blank
                if pd.isna(val):
                    items.append(QStandardItem(''))
                    continue
                # Format scalars
                if col == 'IsOutlier':
                    it = QStandardItem('Yes' if is_outlier else 'No')
                    it.setEditable(True)
                    items.append(it)
                elif isinstance(val, (float, np.floating)):
                    items.append(QStandardItem(f"{val:.3f}"))
                else:
                    items.append(QStandardItem(str(val)))

            # Highlight selected CPM column
            if selected_colname and selected_colname in ordered_cols:
                cidx = ordered_cols.index(selected_colname)
                if 0 <= cidx < len(items):
                    items[cidx].setBackground(QColor("#FFF9C4"))  # light yellow

            # Highlight outlier rows
            if is_outlier:
                for it in items:
                    it.setBackground(QColor("#FFCDD2"))  # light red

            self.modelVial.appendRow(items)

        self.tblVial.resizeColumnsToContents()

        # Optional: reconnect itemChanged for outlier toggling
        def on_item_changed(item):
            col = item.column()
            row_idx = item.row()
            if ordered_cols[col] == 'IsOutlier':
                val = item.text().strip().lower()
                set_true = val in ('yes', 'true', '1', 'y')
                cyc_txt = self.modelVial.item(row_idx, ordered_cols.index('Cycle')).text() if 'Cycle' in ordered_cols else ''
                rep_txt = self.modelVial.item(row_idx, ordered_cols.index('Repeat')).text() if 'Repeat' in ordered_cols else ''
                try:
                    cyc = int(cyc_txt)
                    rep = int(rep_txt)
                except Exception as e:

                    logging.warning(f"Exception caught: {e}"); return
                mask = (self.raw_data['Position'] == self.current_pos) & (self.raw_data['Cycle'] == cyc) & (self.raw_data['Repeat'] == rep)
                self.raw_data.loc[mask, 'IsOutlier'] = set_true
                self._update_vial_table()

        try:
            self.modelVial.itemChanged.disconnect()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        self.modelVial.itemChanged.connect(on_item_changed)

    def _prev_vial(self):
        if self.raw_data is None or self.current_pos is None: return
        all_pos = sorted(self.raw_data['Position'].unique())
        try:
            idx = list(all_pos).index(self.current_pos)
            if idx > 0:
                self.current_pos = all_pos[idx-1]
                self._refresh_vial_views()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    def _next_vial(self):
        if self.raw_data is None or self.current_pos is None: return
        all_pos = sorted(self.raw_data['Position'].unique())
        try:
            idx = list(all_pos).index(self.current_pos)
            if idx < len(all_pos)-1:
                self.current_pos = all_pos[idx+1]
                self._refresh_vial_views()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    def _get_run_isotope_id(self):
        """Infers Isotope from the first Analysis in the LoadList."""
        with db_manager.get_connection() as conn:
            sql = """
                SELECT  Media.MediaID, Media.abbreviation
                FROM TRIMS.LSCRun lr INNER JOIN
                Workflow AS wf ON wf.WorkflowID = lr.WorkflowID
                INNER JOIN Media ON Media.MediaID=wf.MediaID
                WHERE lr.RunID = :rid
            """
            res = conn.execute(text(sql), {'rid': self.run_id}).fetchone()
            return res.MediaID if res else 200 # Default to H-3 if unknown

    def _normalize_quantulus_sourceheader(self, s):
        """Return 'CPM#' or 'SQP' from legacy inputs ('2','F2','CPM_F2','SQP (QIP)')"""
        if s is None:
            return None
        t = str(s).strip().upper()
        if t in ('SQP', 'QIP', 'SQP (QIP)'):
            return 'SQP'
        # accept "2", "F2", "CPM_F2", "CPM2"
        m = re.match(r'^(?:CPM[_\s]*F)?(\d+)$', t) or re.match(r'^CPM\s*(\d+)$', t)
        if m:
            return f'CPM{int(m.group(1))}'
        if re.match(r'^CPM\d+$', t):
            return t
        return None

    def _is_net_field(self, protocol, target_field: str) -> bool:
        """Check if a target field is marked as 'net' in protocol mappings"""
        if not protocol or not protocol.mappings:
            return False
        
        for m in protocol.mappings:
            if m.target_field == target_field:
                return m.is_net
        
        return False
        
    def _start_import(self):
        """
        Start import using protocol-first approach.
        Falls back to old saved_mapping if no protocol available.
        """
        fmt_id = int(self.cmbFormat.currentData() or 0)
        fmt_name = self.cmbFormat.currentText()
        isotope_id = self._get_run_isotope_id()
        
        # Try protocol-based approach first
        try:
            protocol = self._get_current_protocol()
            
            if not protocol:
                raise ValueError("No protocol selected")
            
            # Get mapping from protocol
            mapping_dict = ProtocolManager.get_mapping_dict(protocol)
            
            # For Quantulus files (fmt_id 1 or 2), ensure minimal keys
            if fmt_id in {1, 2}:
                if 'CPM' not in mapping_dict or not mapping_dict['CPM']:
                    mapping_dict['CPM'] = 'CPM1'
                    logging.info("Quantulus: Defaulted CPM → CPM1")
                
                if 'QIP' not in mapping_dict or not mapping_dict['QIP']:
                    mapping_dict['QIP'] = 'SQP'
                    logging.info("Quantulus: Defaulted QIP → SQP")
            
            # Build settings from protocol (use .get() for dict access!)
            settings = {
                'format_name': fmt_name,
                'outlier_method': protocol.settings.get('outlier_method'),
                'outlier_param': protocol.settings.get('outlier_sigma', 2.0),
                'win_cpm': mapping_dict.get('CPM'),
                'win_dpm': mapping_dict.get('DPM'),
                'win_full': mapping_dict.get('Full'),
                'win_qip': mapping_dict.get('QIP'),
                'net_cpm': self._is_net_field(protocol, 'CPM'),
                'net_dpm': self._is_net_field(protocol, 'DPM'),
                'count_time_unit': 1 if self.cmbCountTimeUnit.currentText() == 'Minutes' else 2,
                'mapping': mapping_dict,
                'apply_outliers': False,
                'signal_metric': protocol.settings.get('metric', 'CPM'),
                'eff_source': protocol.settings.get('efficiency_mode', 'Per-cycle (file)'),
                'protocol': protocol,
            }
            
            logging.info(f"OK Using protocol for import: {protocol.name}")
            
        except Exception as e:
            logging.error(f"Protocol not available, using fallback: {e}")
            
            # FALLBACK: Use old saved_mapping approach
            saved_mapping = self._get_mapping(fmt_id, isotope_id)
            
            # For Quantulus, ensure defaults
            if fmt_id in {1, 2}:
                cpm_tok = saved_mapping.get('CPM') or 'CPM1'
                qip_tok = saved_mapping.get('QIP') or 'SQP'
                
                if not saved_mapping.get('CPM') or not saved_mapping.get('QIP'):
                    saved_mapping.update({'CPM': cpm_tok, 'QIP': qip_tok})
                    self._save_mapping(fmt_id, isotope_id, saved_mapping)
                    self._set_mapping(fmt_id, saved_mapping)
            
            mapping_dict = saved_mapping
            
            # Build settings from UI
            settings = {
                'format_name': fmt_name,
                'outlier_method': self.cmbOutlier.currentText(),
                'outlier_param': self.spnThreshold.value(),
                'win_cpm': self.cmbWinCPM.currentData(),
                'win_dpm': self.cmbWinDPM.currentData(),
                'win_full': self.cmbWinFull.currentData(),
                'win_qip': self.cmbWinQIP.currentData(),
                'net_cpm': bool(self.chkNetCPM.isChecked()),
                'net_dpm': bool(self.chkNetDPM.isChecked()),
                'count_time_unit': 1 if self.cmbCountTimeUnit.currentText() == 'Minutes' else 2,
                'mapping': mapping_dict,
                'apply_outliers': False,
                'signal_metric': self.cmbSignalMetric.currentText(),
                'eff_source': self.cmbEffSource.currentText(),
            }
            
            logging.info("OK Using UI-based settings for import")
        
        # Start worker
        self.btnImport.setEnabled(False)
        self.progressBar.setRange(0, 0)
        self.worker = LSCImportWorker(self.run_id, self.txtPath.text(), fmt_id, settings)
        self.worker.progress.connect(self.lblStatus.setText)
        
        def _on_done(success, msg, df):
            self.progressBar.setRange(0, 100)
            self.progressBar.setValue(100 if success else 0)
            self.btnImport.setEnabled(True)
            
            if success:
                self.raw_data = df
                fmt_id_local = int(self.cmbFormat.currentData() or 0)
                isotope_id = self._get_run_isotope_id()
                mapping = self._get_saved_mapping(fmt_id_local, isotope_id)
                self._set_windows_net_checkboxes(mapping)
                
                # Ensure raw_data['CPM'] is set at import end even if mapping was defaulted
                try:
                    m = getattr(self.worker, 'settings', {}).get('mapping', {}) if hasattr(self, 'worker') else {}
                    cpm_sel = m.get('CPM')
                    if cpm_sel and cpm_sel in self.raw_data.columns:
                        self.raw_data['CPM'] = pd.to_numeric(self.raw_data[cpm_sel], errors='coerce')
                    elif 'CPM1' in self.raw_data.columns:
                        # safe fallback
                        self.raw_data['CPM'] = pd.to_numeric(self.raw_data['CPM1'], errors='coerce')
                except Exception as e:
                    logging.error(f"Assign CPM post-import failed: {e}", exc_info=True)

                try:                    
                    if fmt_id_local == 6 and isinstance(self.raw_data, pd.DataFrame):
                        # Load saved mapping (contains CPM/QIP + optional _unc keys)
                        iso_id = self._get_run_isotope_id()
                        m = self._get_saved_mapping(fmt_id_local, iso_id)

                        # Helper to assign column if present
                        def _assign(to_col: str, from_key: str):
                            src = m.get(from_key)
                            if src and src in self.raw_data.columns:
                                self.raw_data[to_col] = pd.to_numeric(self.raw_data[src], errors='coerce')

                        # Prefer explicit mapping; otherwise leave parser defaults
                        _assign('CPM', 'CPM')
                        _assign('QIP', 'QIP')

                        # Uncertainty for CPM if provided (for compute phase)
                        unc_key = m.get('CPM_unc')
                        if unc_key and unc_key in self.raw_data.columns:
                            self.raw_data['UncCPM'] = pd.to_numeric(self.raw_data[unc_key], errors='coerce')
                        # If not provided, later compute path will fall back to NetCPM_unc (file)
                        
                except Exception as e:
                    logging.error(f"HIDEX Matrix post-import mapping failed: {e}", exc_info=True)
                        
                if df is not None and not df.empty:
                    self.current_pos = int(sorted(df['Position'].unique())[0])
                    self._populate_cpm_combo(self.raw_data)
                    self._refresh_vial_views()
                    self.btnCompute.setEnabled(True)   # enable Step 2
                self.lblStatus.setText(msg)
                # QMessageBox.information(self, 'Import Complete', msg)
                self.raise_(); self.activateWindow()
            else:
                QMessageBox.critical(self, 'Error', msg)

        self.worker.finished.connect(_on_done)
        self.worker.start()

    def _populate_preview_table(self):
        """
        Updated preview:
        - CPM-first or DPM-first logic
        - net_cpm = max(0, mean_cpm - bkg_cpm)
        - net_dpm = max(0, mean_dpm - bkg_dpm_equiv)
        - bkg_dpm_equiv = bkg_cpm / eff_used
        - eff_used = eff_cycle OR eff_run depending on UI choice
        - activity computed with _compute_net_activity_dpm_row
        """

        if self.computed_means is None or self.computed_means.empty:
            self.modPreview.appendRow([QStandardItem(
                'Run Step 2 — Compute to view preview results (Net CPM, DPM/kg, Final Activity).'
            )])
            self.tblPreview.resizeColumnsToContents()
            return

        if 'IsOutlier' not in self.raw_data.columns:
            self.raw_data['IsOutlier'] = False

        signal_metric = (self.cmbSignalMetric.currentText() or "CPM").upper()
        eff_source    = (self.cmbEffSource.currentText() or "Per-run (computed)").lower()

        unit_id   = int(self.cmbActivityUnit.currentData() or 1)
        method_id = int(self.cmbEFMethod.currentData() or 2)

        # Headers
        headers = [
            'Position', 'AnalysisID',
            'Mean CPM ± Unc', 'Net CPM', 'Mass (kg)',
            'DPM/kg ± Unc', 'EF ± Unc', 'LC', 'MDA', 'Acceptance',
            'Method', 'Final Activity ± Unc', 'Unit'
        ]
        self.modPreview.clear()
        self.modPreview.setHorizontalHeaderLabels(headers)

        # Means lookup
        means_by_pos = {int(r.Position): r for r in self.computed_means.itertuples(index=False)}

        with db_manager.get_connection() as conn:
            run_row = conn.execute(text(
                "SELECT RunStartTime FROM TRIMS.LSCRun WHERE RunID=:rid"
            ), {'rid': self.run_id}).fetchone()
            run_date = run_row.RunStartTime if run_row else None
            if isinstance(run_date, str):
                try: run_date = datetime.fromisoformat(run_date)
                except: run_date = None

            rows = conn.execute(text("""
                SELECT ll.PositionInRun, ll.AnalysisID, ll.SampleAmount,
                    ll.SampleDiluent, ll.CountTime, ll.SampleType,
                    s.CollectionDate,
                    e.EnrichmentFactor, e.EnrichmentFactorUnc,
                    d.EnrichmentFactor AS H2EF, d.EnrichmentFactorUnc AS H2EFunc
                FROM TRIMS.LSCLoadList ll
                INNER JOIN Analysis a ON ll.AnalysisID = a.AnalysisID
                INNER JOIN Sample   s ON s.SampleID = a.SampleID AND s.Prefix = a.Prefix
                LEFT  JOIN TRIMS.Electrolysis e ON ll.AnalysisID = e.AnalysisID
                LEFT  JOIN TRIMS.DeuteriumEnrichment d ON d.ElectrolysisID = e.ElectrolysisID
                WHERE ll.RunID = :rid
                ORDER BY ll.PositionInRun
            """), {'rid': self.run_id}).fetchall()

            unit_name_row = conn.execute(text(
                "SELECT ShortName FROM MeasurementUnit WHERE UnitID=:u"
            ), {'u': unit_id}).fetchone()
            unit_short = unit_name_row.ShortName if unit_name_row else ''

        # Vol-based uncertainty
        try:
            vol_unc_g = float(get_global_value("VOLUME_UNCERTAINTY_COCKTAIL") or 0.0)
        except:
            vol_unc_g = 0.0

        # Background timing
        bkg_positions = [r.PositionInRun for r in rows if r.SampleType == 1]
        bkg_time_sum = self.raw_data[
            (self.raw_data['Position'].isin(bkg_positions)) &
            (self.raw_data['IsOutlier'] == False)
        ]['CountTime'].sum()
        if bkg_time_sum <= 0:
            bkg_time_sum = 1.0

        bkg_cpm     = float(self.bkg_mean or 0.0)
        bkg_cpm_unc = float(self.bkg_unc  or 0.0)

        # -------------------------
        # MAIN LOOP PER VIAL
        # -------------------------
        for r in rows:
            pos = int(r.PositionInRun)
            mrow = means_by_pos.get(pos)
            if not mrow:
                continue

            mean_cpm = float(mrow.MeanCPM or 0.0)
            unc_cpm  = float(mrow.UncCPM  or 0.0)

            mean_dpm = float(getattr(mrow, "MeanDPM", float("nan")) or 0.0)
            unc_dpm  = float(getattr(mrow, "UncDPM", 0.0) or 0.0)

            # Vial mass
            amt_g = float(r.SampleAmount or 0.0)
            dil_g = float(r.SampleDiluent or 0.0)
            mass_kg = max((amt_g - dil_g), 0.001) / 1000.0

            # Efficiency selection for background conversion
            if eff_source.startswith("per-cycle") and hasattr(mrow, "EffCycle"):
                eff_used = float(mrow.EffCycle or 0.0)
            else:
                eff_used = float(self.eff or 0.0)

            if eff_used <= 0:
                eff_used = 1.0

            # -----------------------------
            #  NET CPM (always in CPM domain)
            # -----------------------------
            net_cpm = mean_cpm - bkg_cpm
            if net_cpm < 0: net_cpm = 0.0

            # -----------------------------
            #  NET DPM
            # -----------------------------
            bkg_dpm_equiv = bkg_cpm / eff_used
            net_dpm = mean_dpm - bkg_dpm_equiv
            if net_dpm < 0: net_dpm = 0.0

            # Massic DPM
            massic_dpm = net_dpm / mass_kg if mass_kg > 0 else 0.0

            # -----------------------------
            #  Activity computation
            # -----------------------------
            if signal_metric == "DPM":
                # use DPM-mode parameters
                A_enr, A_enr_unc = _compute_net_activity_dpm_row(
                    mean_cpm=mean_dpm,
                    unc_cpm=unc_dpm,
                    bkg_cpm=bkg_dpm_equiv,
                    bkg_cpm_unc=bkg_cpm_unc / eff_used,
                    eff_frac=1.0,
                    eff_unc_frac=0.0,
                    sample_amount_g=amt_g,
                    sample_diluent_g=dil_g,
                    sample_amount_unc_g=vol_unc_g,
                )
            else:
                # CPM-mode
                is_cpm_net = getattr(self.raw_data, 'attrs', {}).get('cpm_is_net', False)
                A_enr, A_enr_unc = _compute_net_activity_dpm_row(
                    mean_cpm=mean_cpm,
                    unc_cpm=unc_cpm,
                    bkg_cpm=bkg_cpm if not is_cpm_net else 0.0,
                    bkg_cpm_unc=bkg_cpm_unc,
                    eff_frac=float(self.eff or 0.0),
                    eff_unc_frac=float(self.eff_unc or 0.0),
                    sample_amount_g=amt_g,
                    sample_diluent_g=dil_g,
                    sample_amount_unc_g=vol_unc_g,
                    is_net=is_cpm_net
                )

            # --- Decay correction ---
            df, df_unc = calculate_decay_factor(r.CollectionDate, run_date)
            A_dpm = A_enr / df if df and df > 0 else 0.0
            A_dpm_unc = (
                abs(A_dpm) * (((A_enr_unc / A_enr)**2 + (df_unc / df)**2)**0.5)
                if abs(A_dpm) > 0 and A_enr > 0 and df > 0
                else 0.0
            )

            # ---- EF selection (unchanged) ----
            EF, EF_unc, method_txt = 1.0, 0.0, 'Direct'
            if method_id == 1: # Deuterium
                if getattr(r, 'H2EF', None):
                    EF = float(r.H2EF or 1.0); EF_unc = float(r.H2EFunc or 0.0); method_txt = 'Deuterium'
                elif getattr(r, 'EnrichmentFactor', None):
                    EF = float(r.EnrichmentFactor or 1.0); EF_unc =  float(r.EnrichmentFactorUnc or 0.0); method_txt = 'Spike'
            elif method_id == 2: # Spike
                if getattr(r, 'EnrichmentFactor', None):
                    EF = float(r.EnrichmentFactor or 1.0); EF_unc = float(r.EnrichmentFactorUnc or 0.0); method_txt = 'Spike'
                elif getattr(r, 'H2EF', None):
                    EF = float(r.H2EF or 1.0); EF_unc = float(r.H2EFunc or 0.0); method_txt = 'Deuterium'
            else:
                EF, EF_unc = 1.0, 0.0; method_txt = 'Direct'

            # ---- Pre-enrichment ----
            A_pre = A_dpm / EF if EF > 0 else 0.0
            rel_A = (A_dpm_unc / A_dpm) if A_dpm > 0 else 0.0
            rel_E = (EF_unc / EF) if EF > 0 else 0.0
            A_pre_unc = abs(A_pre) * ((rel_A**2 + rel_E**2)**0.5) if A_pre != 0 else 0.0

            # ---- Final unit conversion ----
            Final, Final_unc = convert_activity_unit(
                unit_from=2, unit_to=unit_id,
                c_value=A_pre, c_unc=A_pre_unc,
                efficiency=float(self.eff or 0.0),
                efficiency_unc=float(self.eff_unc or 0.0),
                return_type=1
            )
            if Final_unc is None or Final == 0:
                Final_unc = 0.0

            # Display strings
            mean_str = f"{mean_cpm:.2f} ± {unc_cpm:.2f}"
            dpm_str  = f"{A_dpm:.2f} ± {A_dpm_unc:.2f}"
            ef_str   = f"{EF:.2f} ± {EF_unc:.2f}"
            fin_str  = f"{Final:.2f} ± {Final_unc:.2f}"

            # ---- LC / MDA ----
            eff_for_ld = float(self.eff or 0.0)
            if eff_for_ld > 1.0:
                eff_for_ld /= 100.0
            if eff_for_ld <= 0:
                eff_for_ld = 0.0001

            ld = lower_detection_limit(
                bkg_cpm=float(self.bkg_mean),
                bkg_time_min=bkg_time_sum,
                sample_time_min=float(r.CountTime or 1.0),
                eff_frac=eff_for_ld,
                vol_kg=mass_kg
            )

            # Divide by EF to get original-sample DPM/kg (matching VBA: dblMDA = MDA / EF)
            ef_safe = EF if EF > 0 else 1.0
            vial_lc  = ld['LC_dpm_per_kg'] / ef_safe
            vial_mda = ld['LD_dpm_per_kg'] / ef_safe

            # Status: compare against A_pre (original-sample DPM/kg, decay-corrected)
            if A_pre >= vial_mda:
                status, color = "Quantifiable", QColor("#C8E6C9")
            elif A_pre >= vial_lc:
                status, color = "Qualitative", QColor("#FFF9C4")
            else:
                status, color = "Below LC", QColor("#EEEEEE")

            items = [
                QStandardItem(str(pos)),
                QStandardItem(str(int(r.AnalysisID)) if r.AnalysisID else ''),
                QStandardItem(mean_str),
                QStandardItem(f"{net_cpm:.2f}"),
                QStandardItem(f"{mass_kg:.4f}"),
                QStandardItem(dpm_str),
                QStandardItem(ef_str),
                QStandardItem(f"{vial_lc:.2f}"),
                QStandardItem(f"{vial_mda:.2f}"),
                QStandardItem(status),
                QStandardItem(method_txt),
                QStandardItem(fin_str),
                QStandardItem(unit_short),
            ]
            items[9].setBackground(color)
            self.modPreview.appendRow(items)

        self.tblPreview.resizeColumnsToContents()

    def _get_historical_efficiency(self, conn):
        """
        Returns the average efficiency from the most recent valid runs
        matching the currently selected format (by name).
        """
        # Get the current format name from the UI
        fmt_name = (self.cmbFormat.currentText() or "").strip().lower()
        if not fmt_name:
            # fallback: return a safe default
            return None

        # Use LIKE for partial matches (robust to custom names)
        sql = f"""
            SELECT {db_manager.sql_top(5)}r.CounterEfficiency
            FROM TRIMS.LSCRun r
            JOIN AnalysisProcedure AS ap ON ap.ProcedureID = r.ProcedureID
            JOIN GUItblFileFormat ff ON ff.lngFormatID = ap.AnalysisImportFormat
            WHERE r.CounterEfficiency > 0 AND r.RunStatus >= 8
            AND LOWER(ff.strFormatName) LIKE :fmt
            ORDER BY r.RunStartTime DESC
            {db_manager.sql_limit(5)}
        """
        # Use wildcards to allow partial matches (e.g., 'hidex', 'quantulus', etc.)
        fmt_like = f"%{fmt_name.split()[0]}%"
        rows = conn.execute(text(sql), {'fmt': fmt_like}).fetchall()
        if not rows:
            return None
        return np.mean([float(r.CounterEfficiency) for r in rows])

    def _plot_calibration_curve(self, hist_eff):
        """Generates a linear regression plot of Standards to verify Efficiency."""
        with db_manager.get_connection() as conn:
            stds_df = self._get_stds_validation_data(conn)
        
        if stds_df is None or stds_df.empty: 
            return

        fig = self.calib_plot_canvas.figure
        fig.clear()
        ax = fig.add_subplot(111)
        
        x = stds_df['TrueDPM'].values
        y = stds_df['NetCPM'].values
        self.calib_positions = stds_df['Position'].values
        
        # 1. Plot standard vials as scatter points
        self.calib_scatter = ax.scatter(x, y, color='blue', picker=5, label='Standards')
        
        # 2. Define the x-range for the lines (from 0 to max DPM)
        x_max = np.max(x) * 1.1 if len(x) > 0 else 1000
        x_line = np.linspace(0, x_max, 100)
        
        # 3. Plot Current Fit (Efficiency)
        # Ensure self.eff is a float and not None
        current_eff = float(self.eff or 0.0)
        ax.plot(x_line, current_eff * x_line, color='blue', 
                alpha=0.6, label=f'Current (E={current_eff:.4f})')
        
        # 4. Plot Historical Reference
        historical_eff = float(hist_eff or 0.0)
        ax.plot(x_line, historical_eff * x_line, color='gray', 
                linestyle='--', label=f'Hist Avg (E={historical_eff:.4f})')
        
        # 5. Force the plot to start at (0,0) and scale properly
        ax.set_xlim(left=0)
        ax.set_ylim(bottom=0)
        
        ax.set_title("Efficiency Calibration Curve")
        ax.set_xlabel("True Activity (DPM)")
        ax.set_ylabel("Net Count Rate (CPM)")
        ax.legend(loc='upper left')
        
        # Re-initialize annotation
        self.calib_annot = ax.annotate("", xy=(0,0), xytext=(15,15),
                                      textcoords="offset points",
                                      bbox=dict(boxstyle="round", fc="w", alpha=0.9),
                                      arrowprops=dict(arrowstyle="->"))
        self.calib_annot.set_visible(False)
        self.calib_plot_canvas.draw()

    def _on_calib_plot_hover(self, event):
        """Interactive tooltip for vial identification."""
        if event.inaxes != self.calib_plot_canvas.figure.axes[0]:
            return
            
        cont, ind = self.calib_scatter.contains(event)
        if cont:
            pos_idx = ind["ind"][0]
            pos_id = int(self.calib_positions[pos_idx])

            # Calculate % deviation for extra context
            true_dpm = self.calib_scatter.get_offsets()[pos_idx][0]
            expected_cpm = true_dpm * self.eff
            actual_cpm = self.calib_scatter.get_offsets()[pos_idx][1]
            dev = ((actual_cpm / expected_cpm) - 1) * 100 if expected_cpm > 0 else 0
            
            self.calib_annot.xy = (event.xdata, event.ydata)
            self.calib_annot.set_text(f"Vial: {pos_id}\nDev: {dev:+.2f}%")
            self.calib_annot.set_visible(True)
            self.calib_plot_canvas.draw_idle()
        else:
            if self.calib_annot.get_visible():
                self.calib_annot.set_visible(False)
                self.calib_plot_canvas.draw_idle()

    def _get_stds_validation_data(self, conn):
        """
        Retrieves and pairs observed Net CPM with True DPM for all standards in the run.
        Used as the input for Chi-squared fit validation.
        """
        # 1. Identify Standards (Type 2) in the current LoadList
        sql = """
            SELECT ll.PositionInRun  AS positioninrun,
                   ll.SampleAmount   AS sampleamount,
                   a.SampleID        AS sampleid
            FROM TRIMS.LSCLoadList ll
            JOIN Analysis a ON ll.AnalysisID = a.AnalysisID
            WHERE ll.RunID = :rid AND ll.SampleType = 2
        """
        with db_manager.get_engine().connect() as _sa_conn:
            stds_info = pd.read_sql(text(sql), _sa_conn, params={'rid': self.run_id})
        
        if stds_info.empty or self.computed_means is None:
            return None

        # 2. Merge with computed means to get observed CPM
        data = self.computed_means.merge(stds_info, left_on='Position', right_on='positioninrun')
        
        # 3. Fetch run date for decay correction
        run_row = conn.execute(text(f"SELECT {db_manager.sql_top(1)}RunStartTime FROM TRIMS.LSCRun WHERE RunID = :rid{db_manager.sql_limit(1)}"),
                            {'rid': self.run_id}).fetchone()
        run_date = run_row.RunStartTime if run_row else datetime.now()

        validation_rows = []
        for _, r in data.iterrows():
            # Get standardized 1000mL concentration DPM/kg
            # DecayCorrected=True ensures we compare against activity at the time of counting
            true_dpm_kg, _, _, _ = get_standard_activity(
                conn, r['sampleid'], run_date, "DPM", Amount=1000, DecayCorrected=True
            )
            
            # Scale to the specific vial mass (kg)
            vial_mass_kg = float(r['sampleamount'] or 0.0) / 1000.0
            total_true_dpm = true_dpm_kg * vial_mass_kg
            
            # Observed Net CPM
            # For HIDEX Matrix: if MeanCPM is from NetCPM_fit, it's ALREADY net (no bkg subtract needed)
            # For other formats: subtract background
            is_hidex_matrix = self._is_hidex_matrix()
            
            if is_hidex_matrix:
                # HIDEX Matrix NetCPM_fit is already background-subtracted by MikroWin
                # Check if we're using a NET column
                fmt_id_check = int(self.cmbFormat.currentData() or 0)
                if fmt_id_check == 6:
                    iso_id = self._get_run_isotope_id()
                    m = self._get_saved_mapping(fmt_id_check, iso_id)
                    cpm_source = m.get('CPM', '')
                    # If user selected a NET column, don't subtract again
                    if 'net' in cpm_source.lower() or 'fit' in cpm_source.lower():
                        net_cpm = max(0, r['MeanCPM'])  # Already NET
                    else:
                        net_cpm = max(0, r['MeanCPM'] - self.bkg_mean)  # Subtract bkg
                else:
                    net_cpm = max(0, r['MeanCPM'])  # Assume already NET for Matrix
            else:
                # Other formats: subtract background normally
                net_cpm = max(0, r['MeanCPM'] - self.bkg_mean)
            
            validation_rows.append({
                'Position': r['Position'],
                'TrueDPM': total_true_dpm,
                'NetCPM': net_cpm
            })

        return pd.DataFrame(validation_rows)

    def _finalize_compute(self, hist_eff):
        """
        Final stage after compute:
        - Update plots
        - Refresh preview table
        - Refresh vial views
        - Compute efficiency deviation and χ² status
        - Refresh QC report tab
        - Enable saving & printing
        """

        with db_manager.get_connection() as conn:
            hist_eff = self._get_historical_efficiency(conn)
            stds_df  = self._get_stds_validation_data(conn)

        if stds_df is not None and not stds_df.empty:
            self.chi_sq = self._perform_statistical_validation(stds_df, hist_eff)
            self._plot_calibration_curve(hist_eff)
        else:
            self.chi_sq = 0        

        # 1) Update chart (means, trends, outliers already baked in)
        self._update_plot()
        # 2) Update preview table (Net CPM / DPM/kg / Activity)
        self._populate_preview_table()
        # 3) Update cycles table & vial views
        self._refresh_vial_views()
        # 4) Compute deviation of efficiency vs historical
        try:
            dev_pct = abs(self.eff - hist_eff) / hist_eff * 100 if hist_eff and hist_eff > 0 else 0
        except Exception:
            dev_pct = 0.0

        # χ² PASS/FAIL
        chi_status = "PASS" if getattr(self, 'chi_sq', 0) <= 11.07 else "FAIL"

        # Extend the existing status label
        self.lblStatus.setText(
            self.lblStatus.text() +
            f"  Eff Dev: {dev_pct:.1f}%  Chi-Sq: {self.chi_sq:.2f} ({chi_status})"
        )

        # 5) Update QC report tab
        self._update_qc_report_tab(hist_eff)
        # 6) Enable save/print buttons
        self.btnPrintQC.setEnabled(True)
        self.btnSaveRun.setEnabled(True)
                
    def _on_compute_clicked(self):
        """Step 2: Unified computation for all formats (Matrix, List, Aloka, Generic)
        Supports unified CPM-first / DPM-first logic, hybrid background rule,
        updated run-efficiency and outlier handling.
        """
        if self.raw_data is None:
            return

        protocol = self._get_current_protocol()
        settings = protocol.settings
        
        # Use protocol's background mode
        if settings.get('background_mode') == 'Calculated':
            self._compute_background_from_samples()
        if settings.get('background_mode') == 'UseFile':
            # Get background from file columns per protocol mappings
            self._compute_background_from_file(protocol)
        elif settings.get('background_mode') == 'Manual':
            # Use manual background value
            self.bkg_mean = settings.get('background_value', 0.0)
            self.bkg_unc = 0.0
            logging.info(f"Using manual background: {self.bkg_mean:.3f}")
        
        # Use protocol's outlier settings
        outlier_method = settings.get('outlier_method')
        outlier_threshold = settings.get('outlier_threshold')
    
        # ----------------------------------------------------------------------
        # Format detection
        # ----------------------------------------------------------------------
        is_matrix  = self._is_hidex_matrix()  # FormatID=6
        is_list    = self._is_hidex_list()    # FormatID=12
        fmt_id     = int(self.cmbFormat.currentData() or 0)

        # ----------------------------------------------------------------------
        # Unified UI settings
        # ----------------------------------------------------------------------
        signal_metric = (self.cmbSignalMetric.currentText() or "CPM").upper()     # "CPM" | "DPM"
        eff_source    = (self.cmbEffSource.currentText() or "").lower()           # "per-cycle (file)" | "per-run (computed)" | variants
        outlier_target = signal_metric       # "CPM" | "DPM"

        # ----------------------------------------------------------------------
        # HIDEX MATRIX (Format 6) — unchanged
        # ----------------------------------------------------------------------
        if is_matrix:
            df = self.raw_data.copy()

            # ----- Respect saved CPM / CPM_unc mapping if present -----
            try:
                fmt_id_local = int(self.cmbFormat.currentData() or 0)
            except Exception:
                fmt_id_local = 0
            if fmt_id_local == 6:
                iso_id = self._get_run_isotope_id()
                m = self._get_saved_mapping(fmt_id_local, iso_id)
                # If mapping chose a specific CPM header, prefer it
                cpm_hdr = m.get('CPM')
                if cpm_hdr and cpm_hdr in df.columns:
                    df['MeanCPM'] = pd.to_numeric(df[cpm_hdr], errors='coerce')
                else:
                    # fallback to your original preference: NetCPM_fit -> CPM
                    if 'NetCPM_fit' in df.columns:
                        df['MeanCPM'] = pd.to_numeric(df['NetCPM_fit'], errors='coerce')
                    elif 'CPM' in df.columns:
                        df['MeanCPM'] = pd.to_numeric(df['CPM'], errors='coerce')
                    else:
                        df['MeanCPM'] = np.nan

                # Uncertainty: mapped column (if any) > file 'NetCPM_unc' > NaN
                unc_hdr = m.get('CPM_unc')
                if unc_hdr and unc_hdr in df.columns:
                    df['UncCPM'] = pd.to_numeric(df[unc_hdr], errors='coerce')
                else:
                    df['UncCPM'] = pd.to_numeric(df.get('NetCPM_unc', np.nan), errors='coerce')
            else:
                # non-ME safeguard (unchanged)
                if 'NetCPM_fit' in df.columns:
                    df['MeanCPM'] = pd.to_numeric(df['NetCPM_fit'], errors='coerce')
                elif 'CPM' in df.columns:
                    df['MeanCPM'] = pd.to_numeric(df['CPM'], errors='coerce')
                else:
                    df['MeanCPM'] = np.nan
                df['UncCPM'] = pd.to_numeric(df.get('NetCPM_unc', np.nan), errors='coerce')

            df['Eff_pct'] = pd.to_numeric(df.get('Eff_pct', np.nan), errors='coerce')

            self.computed_means = df.groupby('Position', as_index=False).agg({
                'MeanCPM': 'first',
                'UncCPM': 'first',
                'QIP': 'first',
                'CountTime': 'first',
                'Eff_pct': 'first'
            })

            # Efficiency & background unchanged; still prefer MikroWin fits if present
            use_eff_pct = ("Per-run" in eff_source and "file" in eff_source)
            if use_eff_pct and df['Eff_pct'].notna().any():
                vals = df['Eff_pct'].dropna().astype(float) / 100.0
                self.eff = float(vals.mean()) if len(vals) > 0 else 0.0
                self.eff_unc = 0.0
            else:
                _, _, self.eff, self.eff_unc = compute_run_params(self.run_id, self.computed_means)

            if 'MeanCPM_BgFit' in df and df['MeanCPM_BgFit'].notna().any():
                self.bkg_mean = float(df['MeanCPM_BgFit'].dropna().iloc[0])
            elif 'MeanCPM_Bg' in df and df['MeanCPM_Bg'].notna().any():
                self.bkg_mean = float(df['MeanCPM_Bg'].dropna().iloc[0])
            else:
                self.bkg_mean = 0.0
            self.bkg_unc = 0.0

            eff_mode = "MikroWin Eff%" if use_eff_pct else "Computed Eff"
            self.lblStatus.setText(f"Compute Success (HIDEX Matrix, {eff_mode}) Eff: {self.eff:.4f}, bkg_mean: {self.bkg_mean:.4f}, ")

        # ----------------------------------------------------------------------
        # HIDEX LIST (Format 12) — unchanged
        # ----------------------------------------------------------------------
        elif is_list:
            df = self.raw_data.copy()

            # Outliers (CPM only)
            settings = {
                'outlier_method': self.cmbOutlier.currentText(),
                'outlier_param':  self.spnThreshold.value(),
                'outlier_on':     "CPM"
            }
            df = self.worker._detect_outliers(df, settings)
            self.raw_data = df

            # Determine CPM window
            mapping_dict = getattr(self.worker, 'settings', {}).get('mapping', {}) if hasattr(self, 'worker') else {}
            cpm_col = mapping_dict.get('CPM')

            roi_cols = [c for c in df.columns if re.fullmatch(r'CPMroi\d+', str(c), flags=re.I)]
            roi_cols = sorted(roi_cols, key=lambda x: int(re.findall(r'\d+', x)[0])) if roi_cols else []

            if not cpm_col or cpm_col not in df:
                if roi_cols:
                    cpm_col = roi_cols[0]
                    if hasattr(self, 'worker'):
                        self.worker.settings.setdefault('mapping', {})['CPM'] = cpm_col
                else:
                    QMessageBox.warning(self, "No CPM Windows Found",
                                        "No CPMroiN columns found. Check file or mapping.")
                    return

            df['CPM'] = pd.to_numeric(df[cpm_col], errors='coerce')
            if df['CPM'].dropna().eq(0).all():
                QMessageBox.warning(self, "No CPM Signal",
                                    f"CPM window '{cpm_col}' contains only zeros/NaN.")
                return

            # Aggregate
            self.computed_means = compute_means(df)
            self.bkg_mean, self.bkg_unc, self.eff, self.eff_unc = compute_run_params(
                self.run_id, self.computed_means)
            self.lblStatus.setText(f"Compute Success (HIDEX List)  Eff: {self.eff:.4f}, bkg_mean: {self.bkg_mean:.4f}, ")

        # ----------------------------------------------------------------------
        # ALOKA (Format 7) — UPDATED with CPM-first & DPM-first unified logic
        # ----------------------------------------------------------------------
        elif fmt_id == 7:
            df = self.raw_data.copy()
            # Outliers (CPM or DPM)
            settings = {
                'outlier_method': self.cmbOutlier.currentText(),
                'outlier_param':  self.spnThreshold.value(),
                'outlier_on':     outlier_target
            }
            df = self.worker._detect_outliers(df, settings)
            self.raw_data = df

            # numeric safety
            for c in ['CPM', 'DPM', 'A_EFF', 'Eff_frac', 'CountTime']:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors='coerce')

            # ----------------------------------------------------------
            # 1) CPM-FIRST LOGIC (same as Quantulus/HIDEX List)
            # ----------------------------------------------------------
            if signal_metric == "CPM":
                self.computed_means = compute_means(df)
                self.bkg_mean, self.bkg_unc, self.eff, self.eff_unc = compute_run_params(
                    self.run_id, self.computed_means)
                self.lblStatus.setText("Compute Success (ALOKA, CPM)")

            else:
                # ----------------------------------------------------------
                # 2) DPM-FIRST LOGIC (hybrid, updated)
                # ----------------------------------------------------------

                # (A) Determine Eff_cycle_i:
                if 'A_EFF' in df.columns and df['A_EFF'].notna().any():
                    df['Eff_frac'] = df['A_EFF'] / 100.0
                else:
                    # Hybrid fallback: 1 if OK else 2
                    # Step 1: try per-cycle Eff_i = DPM/CPM (valid only if DPM and CPM > 0)
                    df['Eff_frac'] = np.where(
                        (df['DPM'] > 0) & (df['CPM'] > 0),
                        df['DPM'] / df['CPM'],
                        np.nan
                    )

                # Compute temporary means for background CPM calculation
                rows = []
                for pos, grp in df.groupby('Position'):

                    mean_cpm = grp['CPM'].mean()
                    unc_cpm  = grp['CPM'].std(ddof=1) if grp['CPM'].count() >= 2 else 0.0

                    mean_dpm = grp['DPM'].mean()
                    unc_dpm  = grp['DPM'].std(ddof=1) if grp['DPM'].count() >= 2 else 0.0

                    # EffCycle: hybrid fallback if needed
                    eff_cycle_vals = grp['Eff_frac'].dropna()
                    if len(eff_cycle_vals) > 0:
                        eff_cycle = eff_cycle_vals.mean()
                        eff_cycle_unc = eff_cycle_vals.std(ddof=1) if len(eff_cycle_vals) >= 2 else 0.0
                    else:
                        # fallback to run-level (temporarily unknown)
                        eff_cycle = np.nan
                        eff_cycle_unc = 0.0

                    rows.append({
                        'Position': int(pos),
                        'MeanCPM': mean_cpm,
                        'UncCPM':  unc_cpm,
                        'MeanDPM': mean_dpm,
                        'UncDPM':  unc_dpm,
                        'EffCycle': eff_cycle,
                        'EffCycleUnc': eff_cycle_unc,
                        'TotalTime': grp['CountTime'].sum(),
                        'Cycles': grp.shape[0]
                    })

                means = pd.DataFrame(rows)
                self.computed_means = means

                # (B) Background from CPM
                self.bkg_mean, self.bkg_unc, eff_run_tmp, eff_unc_tmp = compute_run_params(
                    self.run_id, self.computed_means)

                # (C) eff_run is NOT used for activity in DPM mode
                # but needed for fallback background conversion rule
                # (Option B)
                self.eff = eff_run_tmp
                self.eff_unc = eff_unc_tmp

                # (D) Fallback: fill missing EffCycle using eff_run
                eff_r = float(self.eff or 1.0)
                self.computed_means['EffCycle'] = np.where(
                    self.computed_means['EffCycle'].notna(),
                    self.computed_means['EffCycle'],
                    eff_r
                )

                self.lblStatus.setText("Compute Success (ALOKA, DPM)")

        # ----------------------------------------------------------------------
        # GENERIC FORMAT (no special logic)
        # ----------------------------------------------------------------------
        else:
            settings = {'outlier_method': self.cmbOutlier.currentText(), 'outlier_param': self.spnThreshold.value()}
            self.raw_data = self.worker._detect_outliers(self.raw_data, settings)     
            self.computed_means = compute_means(self.raw_data)
            self.bkg_mean, self.bkg_unc, self.eff, self.eff_unc = compute_run_params(
                self.run_id, self.computed_means)
            self.lblStatus.setText(f"Compute Success  Eff: {self.eff:.4f}, bkg_mean: {self.bkg_mean:.4f}, ")

        # ----------------------------------------------------------------------
        # FINALIZATION: historical efficiency, calibration curve, preview, QC
        # ----------------------------------------------------------------------
        with db_manager.get_connection() as conn:
            hist_eff = self._get_historical_efficiency(conn) or 0
            stds_df  = self._get_stds_validation_data(conn)

        if stds_df is not None and not stds_df.empty:
            self.chi_sq = self._perform_statistical_validation(stds_df, hist_eff)
            self._plot_calibration_curve(hist_eff)
            self.tabsPlots.setCurrentIndex(1)
        else:
            self.chi_sq = 0.0

        dev_pct = abs(self.eff - hist_eff) / hist_eff * 100 if hist_eff > 0 else 0
        chi_status = "PASS" if self.chi_sq <= 11.07 else "FAIL"

        # Preview and UI states
        self._update_plot()
        self._populate_preview_table()
        self._refresh_vial_views()
        dev_pct = abs(self.eff - hist_eff) / hist_eff * 100 if hist_eff > 0 else 0
        chi_status = "PASS" if getattr(self, 'chi_sq', 0) <= 11.07 else "FAIL"
        self.lblStatus.setText(self.lblStatus.text() + f"  Eff Dev: {dev_pct:.1f}%  Chi-Sq: {self.chi_sq:.2f} ({chi_status})")
        self._update_qc_report_tab(hist_eff)
        self.btnPrintQC.setEnabled(True)
        self.btnSaveRun.setEnabled(True)
        
    def _perform_statistical_validation(self, stds_df, hist_eff):
        """
        Performs Chi-squared test using standards data.
        Formula: sum((Observed_i - Expected_i)^2 / Expected_i)
        """
        x_true = stds_df['TrueDPM'].values
        y_obs = stds_df['NetCPM'].values
        y_exp = x_true * self.eff
        
        # Avoid division by zero
        chi_sq = np.sum(((y_obs - y_exp)**2) / (y_exp + 1e-12))
        df_degrees = len(x_true) - 1
        
        # Critical value check (df=5, alpha=0.05 is ~11.07)
        if df_degrees > 0 and chi_sq > 11.07:
            QMessageBox.warning(self, "Chi-Squared Warning", 
                f"Chi-squared ({chi_sq:.2f}) exceeds threshold. The fit to the "
                "calibration curve is statistically poor.")
        
        return float(chi_sq)

    def _prepare_audit_remarks(self):
        """Formats a QC summary for the run Remarks field."""
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        audit = (
            "QC_START\n"
            f"\n--- QC AUDIT {now} ---\n"
            f"Eff: {self.eff:.4f} (±{self.eff_unc:.4f})\n"
            f"Bkg: {self.bkg_mean:.2f} (±{self.bkg_unc:.2f})\n"
            f"Chi-Sq Fit: {getattr(self, 'chi_sq', 0.0):.2f}\n"
            f"Unit: {self.cmbActivityUnit.currentText()}\n"
            f"EF Method: {self.cmbEFMethod.currentText()}\n"
            "QC_END"
        )
        return audit

    def _update_qc_report_tab(self, hist_eff):
        """Generates a text-based report for the third preview tab."""
        if hist_eff is None: hist_eff = 0
        report = [
            "LSC RUN QUALITY CONTROL REPORT",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "-" * 40,
            f"Efficiency (Current):  {self.eff:.4f} ± {self.eff_unc:.4f}",
            f"Efficiency (Hist Avg): {hist_eff:.4f}",
            f"Deviation:             {((self.eff/hist_eff)-1)*100:.2f}%" if hist_eff > 0 else "N/A",
            f"Chi-Squared (χ²):      {getattr(self, 'chi_sq', 0.0):.2f}",
            "-" * 40,
            "DETECTION LIMITS (Run Level):",
            f"Background CPM:        {self.bkg_mean:.2f} ± {self.bkg_unc:.2f}",
            f"Target Unit:           {self.cmbActivityUnit.currentText()}",
            f"Enrichment Method:     {self.cmbEFMethod.currentText()}",
            "-" * 40,
            "VIAL VALIDATION SUMMARY:"
        ]
        
        # Summarize vial statuses
        quantifiable = 0
        below_lc = 0
        
        # We iterate through the preview model to evaluate status manually
        for i in range(self.modPreview.rowCount()):
            # Pull Final Activity and MDA from the preview table
            # Based on your _populate_preview_table headers:
            # Index 8 = Final Activity, Index 9 = Unit (You may need to calculate MDA here)
            
            activity_str = self.modPreview.item(i, 8).text().split('±')[0].strip()
            try:
                activity = float(activity_str)
                # Fetch the run-level MDA for comparison
                # If activity >= run_mda: quantifiable += 1
                if activity > 0: # Placeholder: Replace with actual MDA comparison
                    quantifiable += 1
            except:
                continue
        
        report.append(f"Total Vials: {self.modPreview.rowCount()}")
        report.append(f"Quantifiable: {quantifiable}")
        
        self.txtQCReport.setPlainText("\n".join(report))
        
    def _print_qc_report(self):
        """Generates a PDF with standard point-size text and a scaled plot."""
        file_path, _ = QFileDialog.getSaveFileName(self, "Export QC Report", f"Run_{self.run_id}_QC.pdf", "PDF (*.pdf)")
        if not file_path: return

        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setPageOrientation(QPageLayout.Portrait)
        printer.setOutputFileName(file_path)

        painter = QPainter(printer)
        
        # 1. Standardize the font using Point Size (DPI-independent)
        # Do NOT use setWindow/setViewport here; let the printer handle the scale
        font = QFont("Courier New", 10) 
        font.setPointSize(10) # Explicitly set point size for the printer
        painter.setFont(font)

        # 2. Draw Header and Body Text using standard printer coordinates
        # At HighResolution, coordinates are very large, so we use pageRect margins
        margin = printer.pageRect(QPrinter.DevicePixel).left()
        y_cursor = 500 # High-res start point
        
        painter.drawText(int(margin), y_cursor, f"LSC RUN QC REPORT - RUN {self.run_id}")
        y_cursor += 1000 # Large jump for high-res line spacing

        report_lines = self.txtQCReport.toPlainText().split('\n')
        for line in report_lines:
            painter.drawText(int(margin), y_cursor, line)
            y_cursor += 400 # Line spacing

        # 3. Normalized Scaling for the Image only
        # Switch to 1000-unit virtual window to place the plot
        page_rect = printer.pageRect(QPrinter.DevicePixel).toRect()
        painter.setViewport(page_rect)
        painter.setWindow(0, 0, 1000, 1400) 

        pixmap = self.calib_plot_canvas.grab()
        image = pixmap.toImage()
        # Draw plot in the bottom half (x=100, y=750, width=800, height=550)
        painter.drawImage(QRect(100, 750, 800, 550), image)
        
        painter.end()
        # QMessageBox.information(self, "Success", "QC Report printed with corrected text scaling.")

    def _save_protocol_snapshot_minimal(self):
        try:
            analysis_run_id = getattr(self, 'current_analysis_run_id', None)
            if not analysis_run_id:
                return
            
            protocol = self.current_protocol or Protocol(
                name=f"SIAM Run {analysis_run_id}",
                module='SIAM',
                settings={'instrument_type': str(self.config.instrument_type)}
            )
            
            ProtocolManager.save_run_protocol_snapshot(
                run_id=analysis_run_id,
                protocol=protocol,
                module='SIAM',
                fit_parameters=None,
                was_modified=False,
                user=getpass.getuser()
            )
        except:
            pass  # Don't fail save if snapshot fails
                                
    def _save_run(self):

        """
        Persist current import into TRIMS:
        - HIDEX Matrix (ME): import MikroWin-fitted means directly
        - Other formats: save computed means, run-level params, and final activities

        Guarantees:
        * Uses freshly computed background/efficiency (no stale DB values).
        * Records DataPath and sets RunStatus=8 (Evaluated).
        * Robust to partial/null values; shows user-friendly messages on failure.
        """

        if not getattr(self, "run_id", None):
            QMessageBox.warning(self, "Missing Run", "No RunID is available. Please open a run and try again.")
            return

        fmt_id   = int(self.cmbFormat.currentData() or 0)
        fmt_name = (self.cmbFormat.currentText() or "").lower()
        data_path = self.txtPath.text() if hasattr(self, "txtPath") else ""

        is_hidex_me = (fmt_id == 6) or ("matrix" in fmt_name)

        # ---------------------------
        # HIDEX Matrix (ME) SAVE PATH
        # ---------------------------
        if is_hidex_me:
            try:
                # Persist MikroWin-fitted means exactly as provided by the ME file
                # (CounterEfficiency, MeanBackground fit at run-level; LSCRunMean kinds 0/1/11/90; LoadList Result/Unc/CountTime)
                save_hidex_me_to_db(self.run_id, self.raw_data, data_path=data_path)

                # Optionally trigger activity computation immediately using run-level efficiency we just stored
                with db_manager.get_connection() as conn:
                    row = conn.execute(
                        text("SELECT CounterEfficiency FROM TRIMS.LSCRun WHERE RunID=:rid"),
                        {'rid': self.run_id}
                    ).fetchone()
                    eff_frac = float(getattr(row, 'CounterEfficiency', 0.0) or 0.0)

                    # Use your existing final activity pipeline (unit default = DPM/kg = 2)
                    try:
                        unit = int(getattr(self, 'activity_unit', 2))
                    except Exception:
                        unit = 2
                    calculate_and_save_final_activities(conn, self.run_id, bkg_cpm=0.0, eff_frac=eff_frac,
                                                        activity_unit=unit, used_method=1)
                    conn.commit()

                # UI feedback
                self.lblStatus.setText("HIDEX Matrix means saved to TRIMS (Run, RunMean, LoadList). Activities computed.")
                QMessageBox.information(self, "Saved", "HIDEX Matrix means saved and activities computed.")
                return

            except NameError as ne:
                # Helper not present
                QMessageBox.warning(
                    self, "Save function missing",
                    "save_hidex_me_to_db(...) is not defined. Please add it as discussed, then try again."
                )
                raise
            except Exception as e:
                logging.error(f"HIDEX ME save failed: {e}", exc_info=True)
                QMessageBox.critical(self, "Save failed", f"Could not save HIDEX Matrix means:\n{e}")
                return

        # Basic guards
        if self.raw_data is None or getattr(self.raw_data, "empty", True):
            QMessageBox.warning(self, "Nothing to save", "There is no data to save. Please import first.")
            return
        
        # Small UX improvement: wait cursor while saving
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.btnSaveRun.setEnabled(False)
        try:
            # Persist user prefs / outlier settings
            try:                                
                if self.equipment_id:
                    set_global_value(f"{self.equipment_id}_CPM_OMPTIMIZED_WINDOW", self.cmbWinCPM.currentData(), 'Optimized CPM window')
                    set_global_value(f"{self.equipment_id}_CPM_OPEN_WINDOW",      self.cmbWinFull.currentData(), 'Open/Full spectra window')
                    set_global_value(f"{self.equipment_id}_CPM_QIP_WINDOW",       self.cmbWinQIP.currentData(),  'QIP window')
                    set_global_value(f"{self.equipment_id}_PlotSigma",            self.spnBandSigma.value(),     'Plot sigma for bands')
                with db_manager.get_connection() as conn:
                    conn.execute(text(
                        'UPDATE TRIMS.LSCRun SET OutlierMethod=:m, OutlierSigma=:s WHERE RunID=:rid'
                    ), {'m': self.cmbOutlier.currentText(), 's': self.spnThreshold.value(), 'rid': self.run_id})
                    conn.commit()
            except Exception as e:
                QMessageBox.warning(self, 'Save Settings failed', f'Preferences could not be saved: {e}')
            
            with db_manager.get_connection() as conn:
                # 1. Read only Remarks from DB; use in-memory bkg/eff (already computed this session)
                run_row = conn.execute(text(
                    'SELECT Remarks FROM TRIMS.LSCRun WHERE RunID=:rid'
                ), {'rid': self.run_id}).fetchone()
                # Always use the freshly computed background — never the stale DB value
                bkg_cpm = float(self.bkg_mean or 0.0)
                bkg_unc = float(self.bkg_unc  or 0.0)
                current_remarks = str(run_row.Remarks or "")
                # 2. Use regex to remove existing QC block if it exists
                
                # This regex finds everything between QC_START and QC_END inclusive
                cleaned_remarks = re.sub(r'\[\[QC_START\]\].*?\[\[QC_END\]\]', '', 
                                        current_remarks, flags=re.DOTALL).strip()

                # 3. Combine cleaned user remarks with the fresh QC block
                final_remarks = f"{cleaned_remarks}" #\n\n{new_qc_block}".strip()
                # 2. Map Positions to CountIDs once for fast lookup
                pos2cid = _get_countid_map(conn, self.run_id)

                # 3. DELETE existing data for this run (fast, set-based)
                _bulk_delete_run_mean_and_raw(conn, self.run_id)

                # 4. Batch Insert Raw Cycles to TRIMS.LSCRunRaw
                raw_params = []
                # Map DataFrame cols only once to avoid attribute overhead
                for row in self.raw_data.itertuples(index=False):
                    # row has attributes: Position, Cycle, Repeat, CountTime, CPM, IsOutlier, ...
                    cid = pos2cid.get(int(row.Position))
                    if not cid:
                        continue
                    raw_params.append({
                        'cid': cid,
                        'rep': int(row.Repeat) if row.Repeat is not None else 1,
                        'cyc': int(row.Cycle)  if row.Cycle  is not None else 1,
                        'val': float(row.CPM)  if row.CPM    is not None else 0.0,
                        'rej': bool(getattr(row, 'IsOutlier', False))
                    })

                if raw_params:
                    conn.execute(text("""
                        INSERT INTO TRIMS.LSCRunRaw (CountID, Repeat, CycleNo, ValueKind, CycleValue, IsRejected)
                        VALUES (:cid, :rep, :cyc, 0, :val, :rej)
                    """), raw_params)  # executemany in one round-trip

                # 5. Batch Insert Net Means to TRIMS.LSCRunMean (ValueKind=1)
                is_cpm_net = getattr(self.raw_data, 'attrs', {}).get('cpm_is_net', False)
                formatted_mean_params = []
                for r in self.computed_means.itertuples(index=False):
                    pos = int(r.Position)
                    cid = pos2cid.get(pos)
                    if not cid:
                        continue
                    gross_val = float(r.MeanCPM or 0.0)
                    gross_unc = float(r.UncCPM or 0.0)
                    if is_cpm_net:
                        net_val = gross_val          # already net from instrument
                        net_unc = gross_unc
                    else:
                        net_val = gross_val - bkg_cpm
                        net_unc = float(((gross_unc)**2 + (bkg_unc)**2) ** 0.5)
                    net_val = max(net_val, 0.0)
                    fmt_val, fmt_unc = format_value_uncertainty(net_val, net_unc)
                    formatted_mean_params.append({
                        'cid': cid,
                        'val': fmt_val,
                        'unc': fmt_unc,
                        'rem': 'Net CPM (auto)'
                    })

                now_fn = "NOW()" if db_manager.dialect == "POSTGRESQL" else "GETDATE()"
                usr_fn = "current_user" if db_manager.dialect == "POSTGRESQL" else "SYSTEM_USER"
                conn.execute(text(f"""
                    INSERT INTO TRIMS.LSCRunMean
                        (CountID, ValueKind, MeanValue, MeanValueUnc, Remarks, CreateDateStamp, CreateUserStamp)
                    VALUES
                        (:cid, 1, :val, :unc, :rem, {now_fn}, {usr_fn})
                """), formatted_mean_params)                    

                # 6. Update Run Metadata & Append Audit Remarks
                             
                bkg_formatted, bkg_unc_formatted = format_value_uncertainty(self.bkg_mean, self.bkg_unc)
                eff_formatted, eff_unc_formatted = format_value_uncertainty(self.eff, self.eff_unc)

                conn.execute(text("""
                    UPDATE TRIMS.LSCRun 
                    SET MeanBackground = :b, 
                        MeanBackgroundUnc = :b_unc,
                        CounterEfficiency = :e,
                        CounterEfficiencyUnc = :e_unc,
                        Remarks = :rem, 
                        RunStatus = :s
                    WHERE RunID = :rid
                """), {
                    'b': bkg_formatted, 
                    'b_unc': bkg_unc_formatted,
                    'e': eff_formatted,
                    'e_unc': eff_unc_formatted,
                    'rem': final_remarks, 
                    'rid': self.run_id, 
                    's': 8
                })
                
                # 7. Final Activity Calculation (LSCResult)
                calculate_and_save_final_activities(
                    conn, self.run_id, self.bkg_mean, self.eff,
                    activity_unit=self.cmbActivityUnit.currentData(),
                    used_method=self.cmbEFMethod.currentData()
                )
                # 1. Recalculate Run-Level Detection Limits based on new parameters
                # We use the standardized compute_detection_limit_from_run utility
                
                limits = compute_detection_limit_from_run(conn, run_id=self.run_id)

                mda_formatted = format_for_database(limits['MDA'])
                lc_formatted = format_for_database(limits['LC'])

                conn.execute(text("""
                    UPDATE TRIMS.LSCRun 
                    SET LLD = :mda, LC = :lc 
                    WHERE RunID = :rid
                """), {
                    'mda': mda_formatted, 
                    'lc': lc_formatted, 
                    'rid': self.run_id
                })            
            
                # 8. Batch Update LSCLoadList with Computed Results & Metadata
                # Look up tray capacity so we can set traynumber/positionintray correctly.
                _tc = conn.execute(text(
                    'SELECT tc.vialcapacity FROM public.equipmenttrayconfig tc '
                    'JOIN trims.lscrun r ON r.equipmentid = tc.equipmentid '
                    'WHERE r.runid = :rid'
                ), {'rid': self.run_id}).fetchone()
                vial_capacity = int(_tc.vialcapacity) if _tc else 20

                loadlist_params = []
                for i in range(self.modPreview.rowCount()):
                    pos = int(self.modPreview.item(i, 0).text())
                    aid = int(self.modPreview.item(i, 1).text())

                    dpm_kg_text = self.modPreview.item(i, 5).text().split('±')
                    dpm_kg = float(dpm_kg_text[0].strip())
                    dpm_kg_unc = float(dpm_kg_text[1].strip()) if len(dpm_kg_text) > 1 else 0.0

                    subset = self.raw_data[self.raw_data['Position'] == pos]
                    total_time = subset['CountTime'].sum()

                    tray_num    = (pos - 1) // vial_capacity + 1
                    pos_in_tray = (pos - 1) % vial_capacity + 1

                    loadlist_params.append({
                        'rid':  self.run_id,
                        'pos':  pos,
                        'aid':  aid,
                        'tray': tray_num,
                        'pit':  pos_in_tray,
                        'time': total_time,
                        'res':  dpm_kg,
                        'u':    dpm_kg_unc,
                        'st':   8
                    })

                if loadlist_params:
                    conn.execute(text("""
                        UPDATE TRIMS.LSCLoadList
                        SET TrayNumber     = :tray,
                            PositionInTray = :pit,
                            CountTime      = :time,
                            Result         = :res,
                            ResultUnc      = :u,
                            Status         = :st,
                            IsDecayCorrected = TRUE
                        WHERE RunID = :rid AND PositionInRun = :pos AND AnalysisID = :aid
                    """), loadlist_params)
                    
                conn.commit()

            try:
                protocol = self._get_current_protocol()
                was_modified = not hasattr(self, 'current_protocol') or self.current_protocol is None
                ProtocolManager.save_run_protocol_snapshot(
                    self.run_id, 
                    protocol,
                    module='LSC',
                    was_modified=was_modified,
                    user=self.current_user
                )
            except Exception as e:
                logging.error(f"Failed to save protocol snapshot: {e}")

            self._save_protocol_snapshot_after_processing()
            
            QMessageBox.information(self, 'Saved',
                                    f'Run results saved.\nBkg: {self.bkg_mean:.3f} ± {self.bkg_unc:.3f}\n'
                                    f'Eff: {self.eff:.4f} ± {self.eff_unc:.4f}')
            self.accept()
        except Exception as e:
            logging.error(f'Save Run Failed: {e}', exc_info=True)
            QMessageBox.critical(self, 'Save failed', f'Could not save run results: {e}')
        finally:
            QApplication.restoreOverrideCursor()
            self.btnSaveRun.setEnabled(True)
    
TABLE_STYLE = """
QTableView { border: none; background-color: white; gridline-color: none; }
QTableView::item { padding: 2px 8px; border: none; background-color: white; color: #333333; text-align: left; }
QTableView::item:alternate { background-color: #F3F7FA; }
QTableView::item:selected { background-color: #DDEEFF; color: #000000; }
QHeaderView::section { background-color: white; color: #7F8BB5; font-weight: bold; padding: 4px 8px; border: none; border-bottom: 2px solid #7F8BB5; text-align: left; }
QHeaderView::section:hover { background-color: #DDEEFF; color: #000000; }
"""
META_STYLE = """
QGroupBox { background-color: #FAFAFC; border: 1px solid #D9D9E3; border-radius: 8px; margin-top: 8px; font-weight: 600; padding-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #2D2D33; font-size: 13px; }
QLabel.header-label { color: #3A3A44; font-size: 12.5px; font-weight: 500; }
QLineEdit, QComboBox, QTextEdit, QDateTimeEdit { background: #FFFFFF; border: 1px solid #D0D0DD; border-radius: 6px; padding: 4px 6px; font-size: 12.5px; }
QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QDateTimeEdit:focus { border: 1px solid #4C82FF; outline: none; }
"""

# =============================
# DETAILS WINDOW — styled Metadata + Tabs
# =============================
