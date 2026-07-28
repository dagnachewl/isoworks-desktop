"""
siam_processor_gui.py — SIAM isotope data processor GUI for IsoWorks.
Main processing window that loads raw instrument files, runs calibration and
drift/memory corrections via isotope_processor, and renders interactive plots.
"""
from __future__ import annotations
import sys

def global_exception_handler(exc_type, exc_value, exc_traceback):
    import logging
    logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

import sys
sys.excepthook = global_exception_handler
import os
import webbrowser
import pandas as pd
import numpy as np
import json
import logging
import re
import datetime as dt
import getpass
from typing import Optional, Tuple
from sqlalchemy import text

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QSignalBlocker, QObject, 
    QSettings, QSize, QPropertyAnimation, QEasingCurve, QPoint
)
from PyQt5.QtGui import QColor, QFont

from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QFileDialog, QComboBox, QLabel, QCheckBox, QTableWidget,QDialog,QFormLayout,
                             QTableWidgetItem, QTabWidget, QMessageBox, QGroupBox, QLineEdit,
                             QMenu, QDialogButtonBox, QSpinBox, QHeaderView, QTextBrowser
                            )

from isotope_processor import (load_and_prepare_data, process_isotope_data, Config, 
                                map_and_clean_raw_data, InstrumentType, prepare_raw_data_for_plotting,                               
                                apply_calibration_generic, postprocess_irms, IRMSPostConfig,
                                prepare_ea_single_isotope, read_standards_flexible, compute_validation_results,
                                DEFAULT_MEASURED_ORDER
                               )
from plots import (plot_scatter, plot_timeseries, generate_reports, plot_water_conc_with_stats, 
                   plot_memory_fit, plot_drift_fit, plot_combined_fits,
                   plot_scatter_mpl, plot_timeseries_mpl, plot_water_conc_with_stats_mpl,
                   plot_memory_fit_mpl, plot_drift_fit_mpl, plot_combined_fits_mpl, 
                   plot_irms_calibration_mpl, plot_irms_calibration, plot_cn_scatter_mpl, plot_cn_scatter,
                   plot_linearity_standards, plot_linearity_standards_mpl, plot_drift_standards, plot_drift_standards_mpl,
                   )
from shared_utils import IconCache, set_status, check_employee_privilege, get_current_user_id, normalize_login_name
from help_browser import show_help
import database
from irms.api import load as irms_load
from db_core import db_manager
from protocol_manager import ProtocolManager, Protocol
from protocol_gui import ProtocolEditorDialog, ProtocolGUI
from outliers import OUTLIER_METHODS
from laser_role_validator import validate_laser_roles, Severity, format_issues_for_dialog
from siam_column_resolver import resolve_columns, InstrumentType as ResolverInstrument

from siam_processor.workers.worker import Worker
from siam_processor.models.processor_model import ProcessorModel
from siam_processor.viewmodels.processor_viewmodel import ProcessorViewModel
from siam_processor.views.components import InputPanelBuilder, OutputTabsBuilder
from siam_processor.views.irms_pipeline import IRMSPipelineMixin

class ProcessorWidget(IRMSPipelineMixin, QWidget):

    @property
    def data_file(self): return self.model.data_file
    @data_file.setter
    def data_file(self, val): self.model.data_file = val

    @property
    def standards_file(self): return self.model.standards_file
    @standards_file.setter
    def standards_file(self, val): self.model.standards_file = val

    @property
    def data(self): return self.model.data
    @data.setter
    def data(self, val): self.model.data = val

    @property
    def standards_data(self): return self.model.standards_data
    @standards_data.setter
    def standards_data(self, val): self.model.standards_data = val

    @property
    def injection_data(self): return self.model.injection_data
    @injection_data.setter
    def injection_data(self, val): self.model.injection_data = val

    @property
    def analysis_data(self): return self.model.analysis_data
    @analysis_data.setter
    def analysis_data(self, val): self.model.analysis_data = val

    @property
    def current_isotope(self): return self.model.current_isotope
    @current_isotope.setter
    def current_isotope(self, val): self.model.current_isotope = val

    @property
    def raw_df_ea(self): return self.model.raw_df_ea
    @raw_df_ea.setter
    def raw_df_ea(self, val): self.model.raw_df_ea = val

    @property
    def current_protocol(self): return self.model.current_protocol
    @current_protocol.setter
    def current_protocol(self, val): self.model.current_protocol = val

    @property
    def current_run_id(self): return self.model.current_run_id
    @current_run_id.setter
    def current_run_id(self, val): self.model.current_run_id = val

    @property
    def post_results(self): return self.model.post_results
    @post_results.setter
    def post_results(self, val): self.model.post_results = val

    @property
    def qc_summary(self): return self.model.qc_summary
    @qc_summary.setter
    def qc_summary(self, val): self.model.qc_summary = val

    @property
    def batch_summary(self): return self.model.batch_summary
    @batch_summary.setter
    def batch_summary(self, val): self.model.batch_summary = val

    @property
    def memory_fits(self): return self.model.memory_fits
    @memory_fits.setter
    def memory_fits(self, val): self.model.memory_fits = val

    @property
    def drift_fits(self): return self.model.drift_fits
    @drift_fits.setter
    def drift_fits(self, val): self.model.drift_fits = val

    @property
    def calibration_fits(self): return self.model.calibration_fits
    @calibration_fits.setter
    def calibration_fits(self, val): self.model.calibration_fits = val

    @property
    def multi_iso_raw(self): return self.model.multi_iso_raw
    @multi_iso_raw.setter
    def multi_iso_raw(self, val): self.model.multi_iso_raw = val

    @property
    def multi_iso_inj(self): return self.model.multi_iso_inj
    @multi_iso_inj.setter
    def multi_iso_inj(self, val): self.model.multi_iso_inj = val

    @property
    def multi_iso_analysis(self): return self.model.multi_iso_analysis
    @multi_iso_analysis.setter
    def multi_iso_analysis(self, val): self.model.multi_iso_analysis = val

    @property
    def multi_iso_fits(self): return self.model.multi_iso_fits
    @multi_iso_fits.setter
    def multi_iso_fits(self, val): self.model.multi_iso_fits = val

    @property
    def multi_iso_post(self): return self.model.multi_iso_post
    @multi_iso_post.setter
    def multi_iso_post(self, val): self.model.multi_iso_post = val

    SIDEBAR_WIDTH_EXPANDED = 400
    SIDEBAR_WIDTH_COLLAPSED = 60

    def __init__(self, run_id=None, dsn_path=None):
        super().__init__()      
           
        self.model = ProcessorModel()
        self.viewmodel = ProcessorViewModel(self.model, parent=self)
        self.model.current_run_id = run_id
        
        self.plot_configs = {}
        self.plot_configs_mpl = {}

        self.config = Config()
        self.plot_configs, self.plot_configs_mpl = {}, {}                
        self.is_panel_collapsed = False
        
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # 1. Create the permanent Icon Bar ---
        self.icon_bar = QWidget()
        self.icon_bar.setFixedWidth(self.SIDEBAR_WIDTH_COLLAPSED)
        self.icon_bar.setObjectName("IconBarContainer")
        
        icon_bar_layout = QVBoxLayout(self.icon_bar)
        icon_bar_layout.setContentsMargins(5, 10, 5, 10) # Tight margins
        icon_bar_layout.setSpacing(5)
        
        # 2. Create and move Toggle Button to Icon Bar ---
        self.toggle_sidebar_button = QPushButton()
        self.toggle_sidebar_button.setIcon(IconCache.get_icon("menu"))
        self.toggle_sidebar_button.setIconSize(QSize(24, 24))
        self.toggle_sidebar_button.setFixedSize(QSize(40, 40)) # Square button
        self.toggle_sidebar_button.setObjectName("ToggleSidebarButton")
        self.toggle_sidebar_button.setToolTip("Toggle Input Panel")
        self.toggle_sidebar_button.clicked.connect(self._toggle_input_panel)
        
        icon_bar_layout.addWidget(self.toggle_sidebar_button)
        icon_bar_layout.addStretch(1)
        
        self.help_button = QPushButton()
        self.help_button.setIcon(IconCache.get_icon("computer"))
        self.help_button.setIconSize(QSize(24, 24))
        self.help_button.setFixedSize(QSize(40, 40))
        self.help_button.setObjectName("ToggleSidebarButton")
        self.help_button.setToolTip("Open User Guide (F1)")
        self.help_button.clicked.connect(lambda: show_help(self, "create_siam_run_processor"))

        icon_bar_layout.addWidget(self.help_button)
        main_layout.addWidget(self.icon_bar)
        
        # 3. Setup and Add the Input Panel ---
        self._input_builder = InputPanelBuilder(self)
        self._input_builder.setup_input_panel()
        self.input_panel.setObjectName("InputDockBody")
        self.input_panel.setAttribute(Qt.WA_StyledBackground, True)
        
        # Start expanded
        self.input_panel.setMinimumWidth(0) # Allow collapsing to 0
        self.input_panel.setMaximumWidth(self.SIDEBAR_WIDTH_EXPANDED)
        main_layout.addWidget(self.input_panel)
        
        # 4. Create the right-side content panel ---
        self.content_panel = QWidget()
        self.content_panel.setObjectName("MainContentPanel")
        self.content_layout = QVBoxLayout() 
        self.content_layout.setContentsMargins(10, 10, 10, 10)
        self.content_layout.setSpacing(5)
        self.content_panel.setLayout(self.content_layout)

        self.status_label.setAlignment(Qt.AlignCenter)
        self.content_layout.addWidget(self.status_label)
        
        self._output_builder = OutputTabsBuilder(self)
        self._output_builder.setup_output_tabs()
        self._output_builder.setup_postprocess_tab()
        self.output_tabs.setObjectName("ModuleStack")
        
        self._ensure_active_isotope_selector()
        self.content_layout.addWidget(self.output_tabs, 1)
        
        main_layout.addWidget(self.content_panel, 1) # 1 = stretch
        
        self.panel_animation = QPropertyAnimation(self.input_panel, b"maximumWidth")
        self.panel_animation.setDuration(250)
        self.panel_animation.setEasingCurve(QEasingCurve.InOutQuad)
        self.panel_animation.finished.connect(self._on_panel_animation_finished)
        
        self.post_cfg = IRMSPostConfig()
        
        try:
            self.reset_postprocess(hide_tab=True)
            self._sync_post_tab_visibility()
        except Exception as e:
            logging.warning(f"Post tab init skipped: {e}")

        self.current_isotope = None
        self._isotope_syncing = False

        self.current_protocol = None
        self.protocol_was_modified = False
               
        logging.info("MainWindow setup complete")

        # Debounced auto-plot timer
        self._auto_plot_timer = QTimer(self)
        self._auto_plot_timer.setSingleShot(True)
        self._auto_plot_timer.timeout.connect(self._auto_plot_safe)   
        
        self._ensure_lims_file_display()
        
        self._init_settings()
        self._load_settings()
        self._connect_settings_autosave()
        
        # --- Apply styles to input_panel directly ---
        self.input_panel.setObjectName("InputDockBody")
        self.input_panel.setAttribute(Qt.WA_StyledBackground, True)
        
        self.setStyleSheet("""
            /* --- Set explicit background for the main content area --- */
            #MainContentPanel {
                background-color: #F3F4F6; /* Light grey */
            }
            
            /* ... (status label styles) ... */

            /* --- MODIFIED: Sidebar Styles --- */
            #IconBarContainer {
                background-color: #FFFFFF;  /* Set icon bar to White */
                border-right: 1px solid #D1D5DB; /* Light grey border */
            }
            
            /* We only style the button's 
               background, NOT the icon color. */
            #ToggleSidebarButton {
                border: none;
                border-radius: 4px;
                background-color: transparent;
                /* --- REMOVED 'color' PROPERTY HERE --- */
            }
            #ToggleSidebarButton:hover {
                background-color: #F3F4F6; /* Light grey hover */
            }
        """)

        # After restoring from QSettings:
        try:
            d = self.settings.value("paths/last_data_file", "", type=str) or ""
            s = self.settings.value("paths/last_standards_file", "", type=str) or ""
            if hasattr(self, "data_file_edit"):      self.data_file_edit.setText(d)
            if hasattr(self, "standards_file_edit"): self.standards_file_edit.setText(s)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # If run_id provided, load its protocol
        self._pending_run_id = run_id
        self._pending_dsn_path = dsn_path
                        
        # Wait for database before loading formats/protocol
        if hasattr(db_manager, '_engine') and db_manager._engine is not None:
            # Database already ready
            QTimer.singleShot(100, self._on_database_ready)
        else:
            # Database not ready yet - poll until ready
            self._db_wait_count = 0
            QTimer.singleShot(500, self._check_database_ready)
                                  
        # Privileges
        self.has_admin_priv = False
        self._check_privileges()

        # --- Check for auto-load ---
        if run_id and dsn_path:
            QTimer.singleShot(0, lambda: self._auto_load_lims_run(run_id, dsn_path))
        else:
            self._update_load_both_enabled()
            QTimer.singleShot(0, self._update_load_both_enabled)

        # Connect ViewModel signals
        self.viewmodel.processingFinished.connect(self.on_processing_finished)
        self.viewmodel.processingError.connect(self.on_processing_error)
        self.viewmodel.progressUpdated.connect(self.status_label.setText)

    def _check_privileges(self):
        try:
            user_normalized = normalize_login_name(get_current_user_id())
            self.has_admin_priv = check_employee_privilege(user_normalized, "siamadmin")
            logging.info(f"User {user_normalized}: admin={self.has_admin_priv}")
        except Exception as e:
            logging.error(f"Failed to check privileges: {e}", exc_info=True)
            self.has_admin_priv = False

    def _populate_file_formats(self):
        if hasattr(self, "_input_builder"):
            return self._input_builder._populate_file_formats()

    def _apply_protocol_settings(self, protocol):
        if hasattr(self, "_input_builder"):
            return self._input_builder._apply_protocol_settings(protocol)

    def _build_protocol_from_current_settings(self):
        if hasattr(self, "_input_builder"):
            return self._input_builder._build_protocol_from_current_settings()

    def _check_database_ready(self):
        """Poll until database is ready (called every 500ms)"""
        if hasattr(db_manager, '_engine') and db_manager._engine is not None:
            logging.info("Database is now ready")
            self._on_database_ready()
        else:
            self._db_wait_count += 1
            if self._db_wait_count < 60:  # Max 30 seconds
                QTimer.singleShot(500, self._check_database_ready)
            else:
                logging.error("Database did not become ready within 30 seconds")
                if hasattr(self, 'cmbFileFormat'):
                    self.cmbFileFormat.addItem("(Database timeout)", None)
    
    def _on_database_ready(self):
        """Called when database is confirmed ready"""
        try:
            logging.info("Database ready - loading formats and protocol")
            
            # 1. Populate file formats
            if hasattr(self, '_populate_file_formats'):
                self._populate_file_formats()
            
            # 2. Load protocol for run if provided
            if self._pending_run_id and hasattr(self, 'load_and_apply_protocol_for_run'):
                self.load_and_apply_protocol_for_run(
                    self._pending_run_id,
                    self._pending_dsn_path
                )
            
        except Exception as e:
            logging.error(f"Database ready handler error: {e}", exc_info=True)   
            
    def _get_instrument_type_string(self):
        """Get instrument type as string for protocol settings"""
        if not hasattr(self, 'instrument_combo'):
            return 'Laser'
        
        instrument_text = self.instrument_combo.currentText()
        
        # Map UI text to protocol setting values
        mapping = {
            'LGR': 'Laser',
            'Picarro': 'Laser',
            'IRMS (EA)': 'IRMS_EA',
            'IRMS (Thermo DI)': 'IRMS_DI'
        }
        
        return mapping.get(instrument_text, 'Laser')

    def _get_instrument_type_enum(self):
        """Get InstrumentType enum from combo box"""
        if not hasattr(self, 'instrument_combo'):
            return InstrumentType.LASER
        
        instrument_text = self.instrument_combo.currentText()
        
        # Map UI text to InstrumentType enum
        if instrument_text in ['LGR', 'Picarro']:
            return InstrumentType.LASER
        elif instrument_text == 'IRMS (EA)':
            return InstrumentType.IRMS_EA
        elif instrument_text == 'IRMS (Thermo DI)':
            return InstrumentType.IRMS_DI
        else:
            return InstrumentType.LASER
        
    def get_equipment_for_run(self, run_id: int) -> Optional[Tuple[int, str]]:
        """
        Get equipment information for a SIAM run        
        Args:
            run_id: SIAnalysisRunID
        Returns:
            Tuple of (format_id, equipment_name) or None if not found
        """
        try:
            # Check database ready
            if not hasattr(db_manager, '_engine') or db_manager._engine is None:
                logging.info("Database not ready, cannot get equipment info")
                return None
            
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT EQ.AnalysisImportFormat AS FormatID, EQ.EquipmentName
                    FROM SIAM.SIAnalysisRun SR 
                    INNER JOIN Equipment EQ ON SR.EquipmentID = EQ.EquipmentID
                    WHERE SR.SIAnalysisRunID = :rid
                """), {"rid": run_id}).fetchone()
                
                if not row:
                    logging.warning(f"No equipment found for run {run_id}")
                    return None
                
                # Use index access (SQL Server compatibility)
                format_id = row[0]  # AnalysisImportFormat
                equipment_name = row[1] if row[1] else ''  # EquipmentName
                
                if not format_id:
                    logging.warning(f"Equipment '{equipment_name}' has no AnalysisImportFormat configured")
                    return None
                
                logging.info(f"Run {run_id}: Equipment='{equipment_name}', FormatID={format_id}")
                return (format_id, equipment_name)
                
        except Exception as e:
            logging.error(f"Failed to get equipment for run {run_id}: {e}", exc_info=True)
            return None

    def load_default_protocol_for_run(self, run_id: int) -> Optional[Protocol]:
        """
        Load the default protocol for the equipment used in a run
        Args:
            run_id: SIAnalysisRunID
        Returns:
            Protocol object or None
        Flow:
            1. Query equipment info (FormatID, EquipmentName) for run
            2. Load default protocol for that FormatID
            3. Return protocol or None
        """
        try:
            # Get equipment info
            equipment_info = self.get_equipment_for_run(run_id)
            if not equipment_info:
                return None
            
            format_id, equipment_name = equipment_info
            
            # Load default protocol for this format
            protocol = ProtocolManager.get_default_protocol('SIAM', format_id)
            
            if protocol:
                logging.info(f"OK Loaded protocol '{protocol.name}' for '{equipment_name}'")
            else:
                logging.info(f"No default protocol for format {format_id} ({equipment_name})")
            
            return protocol
            
        except Exception as e:
            logging.error(f"Failed to load protocol for run {run_id}: {e}", exc_info=True)
            return None

    def detect_instrument_from_equipment(self, equipment_name: str) -> str:
        """
        Detect instrument type from equipment name
        
        Args:
            equipment_name: Equipment name from database
            
        Returns:
            Instrument type: 'LGR', 'Picarro', 'IRMS (EA)', 'IRMS (Thermo DI)', or first match
        """
        if not equipment_name:
            return 'Picarro'  # Default fallback
        
        name_upper = equipment_name.upper()
        
        # Check for known patterns
        if 'LGR' in name_upper or 'LOS GATOS' in name_upper:
            return 'LGR'
        elif 'PICARRO' in name_upper:
            return 'Picarro'
        elif 'EA' in name_upper or 'ELEMENTAL' in name_upper:
            return 'IRMS (EA)'
        elif 'DI' in name_upper or 'DUAL INLET' in name_upper or 'THERMO' in name_upper:
            return 'IRMS (Thermo DI)'
        
        # Fallback: return first word as instrument type
        first_word = equipment_name.split()[0] if equipment_name.split() else 'Unknown'
        logging.warning(f"Unknown equipment type '{equipment_name}', using '{first_word}'")
        return first_word

    def load_and_apply_protocol_for_run(self, run_id: int, dsn_path: str = None):
        """
        Load protocol for run based on equipment configuration
        
        Args:
            run_id: SIAnalysisRunID
            dsn_path: DSN file path (not used, equipment comes from database)
        """
        try:
            # Get equipment info from database
            equipment_info = self.get_equipment_for_run(run_id)
            if not equipment_info:
                logging.info(f"No equipment for run {run_id}, using current settings")
                return False
            
            format_id, equipment_name = equipment_info
            
            # Set file format combo (this triggers _on_format_changed)
            idx = self.cmbFileFormat.findData(format_id)
            if idx >= 0:
                self.cmbFileFormat.setCurrentIndex(idx)
                logging.info(f"Set format to: {self.cmbFileFormat.itemText(idx)}")
            else:
                logging.warning(f"Format {format_id} not in combo")
            
            return True
            
        except Exception as e:
            logging.error(f"Load protocol for run error: {e}", exc_info=True)
            return False

        
    def _get_current_protocol(self) -> Protocol:
        """
        Get current protocol (loaded or build from Config).
        
        Returns:
            Protocol object representing current configuration
        """
        if self.current_protocol:
            return self.current_protocol
        
        return self._build_protocol_from_current_settings()
   
    def _select_protocol(self):
        """
        Select and apply a SIAM protocol.
        Similar to LSC _select_protocol function.
        """
        # Get instrument type for filtering
        instrument_type = self._get_instrument_type_string()
        
        # dlg = ProtocolGUI(self, module='SIAM')
        headers = None
        if hasattr(self, 'data_file') and self.data_file:
            headers = self._peek_columns(self.data_file)

        # Open protocol manager with headers
        dlg = ProtocolGUI(
            self, 
            module='SIAM',
            file_headers=headers,
            restrict_to_module=True
        )        
        if dlg.exec_() == QDialog.Accepted:
            if hasattr(dlg, 'selected_protocol') and dlg.selected_protocol:
                # Apply the protocol
                self.current_protocol = dlg.selected_protocol
                self._apply_protocol_settings(dlg.selected_protocol)
                self.protocol_was_modified = False
                
                QMessageBox.information(
                    self, "Protocol Applied",
                    f"Applied protocol: {dlg.selected_protocol.name}\n\n"
                    f"Settings have been updated."
                )
                
                logging.info(f"User selected protocol: {dlg.selected_protocol.name}")

    def _open_protocol_manager(self):
        """
        Open protocol manager dialog for SIAM protocols.
        Allow user to load/edit/create SIAM protocols.
        """
        
        # Extract headers from loaded file
        headers = None
        if hasattr(self, 'data_file') and self.data_file:
            headers = self._peek_columns(self.data_file)

        current_format_id = self.cmbFileFormat.currentData() if hasattr(self, 'cmbFileFormat') else None
        protocol = ProtocolManager.get_default_protocol('SIAM', current_format_id)
        current_id = None
        if protocol:
            current_id =protocol.id  
                
        dlg = ProtocolGUI(  # or ProtocolGUI
            self, 
            module='SIAM',
            current_protocol_id=current_id,
            file_headers=headers,
            restrict_to_module=True
        )
        
        if dlg.exec_() == QDialog.Accepted:
            if hasattr(dlg, 'selected_protocol') and dlg.selected_protocol:
                self.current_protocol = dlg.selected_protocol
                self._apply_protocol_settings(dlg.selected_protocol)
                self.protocol_was_modified = False
                
                QMessageBox.information(
                    self, "Protocol Loaded",
                    f"Loaded protocol: {self.current_protocol.name}\n\n"
                    "Settings have been applied to the UI."
                )
                
                logging.info(f"User loaded protocol: {self.current_protocol.name}")

    def _create_new_protocol(self):
        """
        Create a new SIAM protocol from current Config settings.
        Opens editor dialog for user to customize.
        """
        # Build protocol from current config
        protocol = self._build_protocol_from_current_settings()
        protocol.name = "New SIAM Protocol"
        protocol.description = ""
        
        # Open editor
        dlg = ProtocolEditorDialog(self, protocol=protocol)
        if dlg.exec_() == QDialog.Accepted:
            self.current_protocol = dlg.protocol
            QMessageBox.information(
                self, "Protocol Saved",
                f"Protocol '{self.current_protocol.name}' saved successfully!"
            )

    def _save_protocol_snapshot_after_processing(self, analysis_run_id: int):
        """
        Save complete protocol snapshot after data processing.
        Should be called after processing completes successfully.
        
        Args:
            analysis_run_id: SIAnalysisRun.SIAnalysisRunID
        """
        try:
            protocol = self._get_current_protocol()
            
            # Build fit parameters from processing results
            fit_parameters = self._build_fit_parameters()
            
            # Save snapshot
            ProtocolManager.save_run_protocol_snapshot(
                run_id=analysis_run_id,
                protocol=protocol,
                module='SIAM',
                fit_parameters=fit_parameters,
                was_modified=self.protocol_was_modified,
                user=getpass.getuser()
            )
            
            logging.info(f"Saved SIAM protocol snapshot for analysis run {analysis_run_id}")
            
        except Exception as e:
            logging.error(f"Failed to save protocol snapshot: {e}", exc_info=True)


    def _build_fit_parameters(self) -> dict:
        """
        Build fit_parameters dict from processing results.
        This captures all correction fit results for the protocol snapshot.
        
        Returns:
            Dictionary with drift_fit, memory_fit, linearity_fit, calibration_fit
        """
        fit_params = {}
        
        # Drift fit (if available)
        if hasattr(self, 'drift_results') and self.drift_results:
            drift_fit = {}
            for isotope, result in self.drift_results.items():
                drift_fitisotope = {
                    'method': result.get('method', 'linear'),
                    'slope': float(result.get('slope', 0)),
                    'intercept': float(result.get('intercept', 0)),
                    'r_squared': float(result.get('r_squared', 0)),
                    'time_range': result.get('time_range', []),
                    'standards_used': result.get('standards_used', [])
                }
            fit_params['drift_fit'] = drift_fit
        
        # Memory fit (if available)
        if hasattr(self, 'memory_results') and self.memory_results:
            memory_fit = {}
            for isotope, result in self.memory_results.items():
                memory_fitisotope = {
                    'method': result.get('method', 'exponential'),
                    'tau': float(result.get('tau', 0)),
                    'amplitude': float(result.get('amplitude', 0)),
                    'r_squared': float(result.get('r_squared', 0)),
                    'memory_std_id': result.get('memory_std_id', '')
                }
            fit_params['memory_fit'] = memory_fit
        
        # Linearity fit (NEW v2.0 - if available)
        if hasattr(self, 'linearity_results') and self.linearity_results:
            linearity_fit = {}
            for isotope, result in self.linearity_results.items():
                linearity_fitisotope = {
                    'method': result.get('method', 'two_point'),
                    'slope': float(result.get('slope', 1.0)),
                    'intercept': float(result.get('intercept', 0)),
                    'r_squared': float(result.get('r_squared', 0)),
                    'concentration_range': result.get('concentration_range', []),
                    'standards_used': result.get('standards_used', []),
                    'concentrations': result.get('concentrations', []),
                    'measured_deltas': result.get('measured_deltas', []),
                    'true_deltas': result.get('true_deltas', [])
                }
            fit_params['linearity_fit'] = linearity_fit
        
        # Calibration fit (if available)
        if hasattr(self, 'calibration_results') and self.calibration_results:
            calibration_fit = {}
            for isotope, result in self.calibration_results.items():
                calibration_fitisotope = {
                    'method': result.get('method', 'two_point'),
                    'slope': float(result.get('slope', 1.0)),
                    'intercept': float(result.get('intercept', 0)),
                    'r_squared': float(result.get('r_squared', 0)),
                    'standards_used': result.get('standards_used', []),
                    'measured_deltas': result.get('measured_deltas', []),
                    'true_deltas': result.get('true_deltas', [])
                }
            fit_params['calibration_fit'] = calibration_fit
        
        # Metadata
        fit_params['metadata'] = {
            'samples_processed': getattr(self, 'samples_processed', 0),
            'standards_count': getattr(self, 'standards_count', 0),
            'instrument_serial': getattr(self, 'instrument_serial', ''),
            'data_file': getattr(self, 'loaded_file_path', '')
        }
        
        return fit_params
    
    def _ensure_lims_controls(self):
        """Build LIMS controls once; usable by all instruments."""

        if getattr(self, "lims_based_widget", None):
            return

        self.lims_based_widget = QGroupBox("LIMS", self)
        v = QVBoxLayout(self.lims_based_widget)

        # Row 1: DSN file
        dsn_label_layout = QHBoxLayout()
        dsn_label_layout.setContentsMargins(0, 0, 0, 0)
        dsn_label_layout.addWidget(QLabel("DSN file:"))

        browse = QPushButton("...", self.lims_based_widget)
        
        def _pick_dsn():
            # start in last-used LIMS folder if available
            try:
                if hasattr(self, "_dialog_start_dir"):
                    start_dir = self._dialog_start_dir("paths/lims_dir")
                else:
                    s = getattr(self, "settings", None)
                    start_dir = s.value("paths/lims_dir", type=str) if s else ""
                    if not (start_dir and os.path.isdir(start_dir)):
                        start_dir = os.path.expanduser("~")
            except Exception:
                start_dir = ""

            path, _ = QFileDialog.getOpenFileName(
                self, "Select DSN", start_dir, "DSN Files (*.dsn);;All Files (*)"
            )
            if not path:
                return

            self.dsn_edit.setText(path)

            # persist DSN file + its folder
            try:
                if hasattr(self, "_record_browse_result"):
                    self._record_browse_result(path, dir_key="paths/lims_dir", file_key="lims/dsn_path")
                else:
                    s = getattr(self, "settings", None)
                    if s:
                        s.setValue("lims/dsn_path", path)
                        s.setValue("paths/lims_dir", os.path.dirname(path))
                        s.sync()
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        
        browse.clicked.connect(_pick_dsn)        
        # dsn_label_layout.addWidget(browse)
        dsn_label_layout.addStretch(1)
        v.addLayout(dsn_label_layout)

        # Row 1.5: DSN file (Input Box only)
        row1w =  QWidget(self.lims_based_widget)
        row1 = QHBoxLayout(row1w); row1.setContentsMargins(0, 0, 0, 0)
        self.dsn_edit = QLineEdit(row1w) #self.lims_based_widget)
        row1.addWidget(self.dsn_edit, 1)
        row1.addWidget(browse)
        v.addWidget(row1w)
               
        # Row 2: Run/Batch + Load
        row2w = QWidget(self.lims_based_widget)
        row2 = QHBoxLayout(row2w); row2.setContentsMargins(0, 0, 0, 0)
        row2.addWidget(QLabel("Run ID / Batch:", row2w))
        self.run_id_edit = QLineEdit(row2w)
        row2.addWidget(self.run_id_edit, 1)

        load_btn = QPushButton("Load Data", row2w)
        load_btn.clicked.connect(self.load_batch_from_lims)
        row2.addWidget(load_btn)
        v.addWidget(row2w)

        # Row 3: Selected data file (hidden until set)
        row3w = QWidget(self.lims_based_widget)
        row3 = QHBoxLayout(row3w); row3.setContentsMargins(0, 0, 0, 0)
        row3.addWidget(QLabel("Selected data file:", row3w))
        self.lims_data_file_edit = QLineEdit(row3w)
        self.lims_data_file_edit.setObjectName("lims_data_file_edit")
        self.lims_data_file_edit.setReadOnly(True)
        self.lims_data_file_edit.setPlaceholderText("—")
        row3.addWidget(self.lims_data_file_edit, 1)
        row3w.setVisible(False)  # hidden until populated
        self.lims_file_row = row3w
        v.addWidget(row3w)
        
        self.dsn_edit.textChanged.connect(self._update_save_to_db_enabled)
        self.run_id_edit.textChanged.connect(self._update_save_to_db_enabled)
        self._update_save_to_db_enabled()
        
    def _auto_load_lims_run(self, run_id, dsn_path):
        """
        Called on startup if run_id and dsn_path are provided.
        Sets UI to LIMS mode, fills fields, and triggers load.
        """
        try:
            logging.info(f"Auto-loading LIMS Run {run_id} with DSN {dsn_path}")
            
            # 1. Force UI to LIMS mode
            if hasattr(self, "option_combo"):
                self.option_combo.setCurrentText("LIMS-Based")
                # Manually trigger the UI refresh
                self.on_option_changed("LIMS-Based") 
            
            # 2. Set DSN and Run ID fields
            if hasattr(self, "dsn_edit"):
                self.dsn_edit.setText(dsn_path)
            if hasattr(self, "run_id_edit"):
                self.run_id_edit.setText(str(run_id))
            
            # 3. Trigger the LIMS load
            if hasattr(self, "load_batch_from_lims"):
                # This function already handles file dialogs etc.
                self.load_batch_from_lims()
            else:
                logging.error("Auto-load failed: load_batch_from_lims method not found.")

        except Exception as e:
            logging.error(f"Auto-load LIMS run failed: {e}", exc_info=True)
            QMessageBox.critical(self, "Auto-Load Error", f"Failed to auto-load LIMS run {run_id}:\n{e}")

    def _init_settings(self):
        # Keep these constant for your app name & org
        self.settings = QSettings("YourLab", "IsotopeApp")

    def _set_combo_text(self, combo, text):
        if combo is None or not text:
            return
        try:
            with QSignalBlocker(combo):
                idx = combo.findText(text)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                else:
                    combo.setCurrentText(text)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
            
    def _load_cfg_from_settings(self):
        
        # Persisted EA (IRMS) post-config items
        try:
            from isotope_processor import IRMSPostConfig
        except Exception:
            class IRMSPostConfig: pass
        if not hasattr(self, "post_cfg") or self.post_cfg is None:
            self.post_cfg = IRMSPostConfig()

        s = getattr(self, "settings", None)
        if s is None:
            return
        try:
            import json
            v = s.value("irms/robust_fits", type=bool)
            if v is not None:
                setattr(self.post_cfg, "robust_fits", bool(v))
            v = s.value("irms/blank_thresholds", type=str)
            if v:
                setattr(self.post_cfg, "blank_thresholds", json.loads(v))
            v = s.value("irms/ea_peak_preference", type=str)
            if v:
                d = json.loads(v)
                d = {str(k): int(vv) for k, vv in d.items() if str(vv).strip() != ""}
                setattr(self.post_cfg, "ea_peak_preference", d)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    def _save_cfg_to_settings(self):
        
        s = getattr(self, "settings", None)
        if s is None: return
        try:
            import json
            s.setValue("irms/robust_fits", bool(getattr(self.post_cfg, "robust_fits", False)))
            s.setValue("irms/blank_thresholds", json.dumps(getattr(self.post_cfg, "blank_thresholds", {}) or {}))
            s.setValue("irms/ea_peak_preference", json.dumps(getattr(self.post_cfg, "ea_peak_preference", {}) or {}))
            s.sync()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
            
    def _load_settings(self):
        s = getattr(self, "settings", None)
        if s is None:
            return
        # touch keys so they're created; nothing to do here beyond ensuring they exist
        _ = s.value("paths/data_dir", type=str)
        _ = s.value("paths/standards_dir", type=str)
        # Window geometry/state
        try:
            geo = s.value("window/geometry")
            if geo is not None: self.restoreGeometry(geo)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        # Instrument & Active Isotope
        try:
            instr = s.value("ui/instrument_text", type=str)
            if instr: self._set_combo_text(getattr(self, "instrument_combo", None), instr)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try:
            active_iso = s.value("ui/active_isotope", type=str)
            if active_iso:
                self.current_isotope = active_iso
                self._set_combo_text(getattr(self, "active_iso_combo", None), active_iso)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # EA options
        try: self._set_combo_text(getattr(self, "ea_linearity_combo", None), s.value("ea/linearity", type=str))
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try: self._set_combo_text(getattr(self, "ea_drift_combo", None), s.value("ea/drift", type=str))
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try:
            chk = getattr(self, "ea_robust_check", None)
            if chk is not None:
                val = s.value("ea/robust", type=bool)
                if val is not None:
                    with QSignalBlocker(chk):
                        chk.setChecked(bool(val))
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # LASER options (support multiple widget names defensively)
        laser_mem = s.value("laser/memory_method", type=str)
        for name in ("laser_memory_combo", "memory_combo"):
            try: self._set_combo_text(getattr(self, name, None), laser_mem)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        for key, names in (
            ("laser/linearity", ("laser_linearity_combo", "linearity_combo")),
            ("laser/drift",     ("laser_drift_combo",     "drift_combo")),
            ("laser/mwl",       ("laser_mwl_combo",       "mwl_combo")),
        ):
            try:
                val = s.value(key, type=str)
                for nm in names:
                    self._set_combo_text(getattr(self, nm, None), val)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        try:
            oc = getattr(self, "outlier_check", None)
            if oc is not None:
                v = s.value("laser/outlier_enabled", type=bool)
                if v is not None:
                    oc.setChecked(bool(v))
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try:
            oedit = getattr(self, "outlier_factor_edit", None)
            if oedit is not None:
                v = s.value("laser/outlier_sd", type=str)
                if v: oedit.setText(v)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # LIMS fields (if present)
        try:
            if hasattr(self, "dsn_edit"):
                v = s.value("lims/dsn_path", type=str)
                if v:
                    with QSignalBlocker(self.dsn_edit): self.dsn_edit.setText(v)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try:
            if hasattr(self, "run_id_edit"):
                v = s.value("lims/run_id", type=str)
                if v:
                    with QSignalBlocker(self.run_id_edit): self.run_id_edit.setText(v)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # Last used paths
        try:
            if hasattr(self, "standards_file_edit"):
                v = s.value("paths/standards_file", type=str)
                if v:
                    with QSignalBlocker(self.standards_file_edit): self.standards_file_edit.setText(v)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try:
            if hasattr(self, "data_file_edit"):
                v = s.value("paths/last_data_file", type=str)
                if v:
                    with QSignalBlocker(self.data_file_edit): self.data_file_edit.setText(v)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # Load IRMS cfg items
        self._load_cfg_from_settings()

    def _save_settings(self):
        s = getattr(self, "settings", None)
        if s is None:
            return

        # Window geometry/state
        try:
            s.setValue("window/geometry", self.saveGeometry())
            # --- MODIFIED: Removed Dock saveState ---
            # if hasattr(self, "saveState"):
            #     s.setValue("window/state", self.saveState())
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # Combos / toggles (EA)
        try: s.setValue("ui/instrument_text", getattr(self.instrument_combo, "currentText", lambda: "")())
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try: s.setValue("ui/active_isotope", getattr(self.active_iso_combo, "currentText", lambda: "")())
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try: s.setValue("ea/linearity", getattr(self.ea_linearity_combo, "currentText", lambda: "")())
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try: s.setValue("ea/drift", getattr(self.ea_drift_combo, "currentText", lambda: "")())
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try:
            chk = getattr(self, "ea_robust_check", None)
            if chk is not None: s.setValue("ea/robust", bool(chk.isChecked()))
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # LASER
        try:
            cb = getattr(self, "laser_memory_combo", None) or getattr(self, "memory_combo", None)
            s.setValue("laser/memory_method", cb.currentText() if cb else "")
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        for key, nm in (("laser/linearity","laser_linearity_combo"),
                        ("laser/drift","laser_drift_combo"),
                        ("laser/mwl","laser_mwl_combo")):
            try:
                cb = getattr(self, nm, None) or getattr(self, nm.replace("laser_",""), None)
                if cb: s.setValue(key, cb.currentText())
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        try:
            oc = getattr(self, "outlier_check", None)
            if oc is not None:
                s.setValue("laser/outlier_enabled", bool(oc.isChecked()))
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try:
            oedit = getattr(self, "outlier_factor_edit", None)
            if oedit is not None:
                s.setValue("laser/outlier_sd", oedit.text())
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # LIMS
        try: s.setValue("lims/dsn_path", self.dsn_edit.text() if getattr(self, "dsn_edit", None) else "")
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try: s.setValue("lims/run_id", self.run_id_edit.text() if getattr(self, "run_id_edit", None) else "")
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # Paths
        try: s.setValue("paths/standards_file", getattr(self, "standards_file", "") or getattr(self.standards_file_edit, "text", lambda: "")())
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try: s.setValue("paths/last_data_file", getattr(self.data_file_edit, "text", lambda: "")())
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # IRMS cfg items
        self._save_cfg_to_settings()

        s.sync()

    def _connect_settings_autosave(self):
        
        # Optional: persist on-the-fly when users tweak common controls
        try:
            if hasattr(self, "instrument_combo"):
                self.instrument_combo.currentTextChanged.connect(lambda _=None: self._save_settings())
            if hasattr(self, "active_iso_combo"):
                self.active_iso_combo.currentTextChanged.connect(lambda _=None: self._save_settings())
            for nm in ("ea_linearity_combo","ea_drift_combo"):
                cb = getattr(self, nm, None)
                if cb: cb.currentTextChanged.connect(lambda _=None: self._save_settings())
            if hasattr(self, "ea_robust_check"):
                self.ea_robust_check.toggled.connect(lambda _=None: self._save_settings())
            for nm in ("laser_memory_combo","laser_linearity_combo","laser_drift_combo","laser_mwl_combo","memory_combo","linearity_combo","drift_combo","mwl_combo"):
                cb = getattr(self, nm, None)
                if cb: cb.currentTextChanged.connect(lambda _=None: self._save_settings())
            if hasattr(self, "outlier_check"):
                self.outlier_check.toggled.connect(lambda _=None: self._save_settings())
            if hasattr(self, "outlier_factor_edit"):
                self.outlier_factor_edit.textChanged.connect(lambda _=None: self._save_settings())
            if hasattr(self, "dsn_edit"):
                self.dsn_edit.textChanged.connect(lambda _=None: self._save_settings())
            if hasattr(self, "run_id_edit"):
                self.run_id_edit.textChanged.connect(lambda _=None: self._save_settings())
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        
    def closeEvent(self, event):
        # (This function is unchanged, still calls _save_settings)
        try:
            self._save_settings()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        super().closeEvent(event)

    def _ensure_lims_file_display(self):
        """Create a hidden 'Selected data file' row under the LIMS section (once)."""
        if hasattr(self, "lims_file_row"):
            return
        from PyQt5.QtWidgets import QWidget, QHBoxLayout, QLabel, QLineEdit

        row = QWidget(self)
        row.setObjectName("lims_file_row")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lab = QLabel("Selected data file:")
        edit = QLineEdit()
        edit.setObjectName("lims_data_file_edit")
        edit.setReadOnly(True)
        edit.setPlaceholderText("—")
        lay.addWidget(lab)
        lay.addWidget(edit, 1)
        row.setVisible(False)

        self.lims_file_row = row
        self.lims_data_file_edit = edit

        # Try to attach to your existing LIMS layout; fall back to input panel.
        parent_layout = (
            getattr(self, "lims_options_layout", None)
            or getattr(self, "lims_layout", None)
            or getattr(self, "input_options_layout", None)
            or getattr(self, "input_panel_layout", None)
        )
        if parent_layout:
            parent_layout.addWidget(row)

    def _lims_set_data_file_path(self, path: str | None):
        """Populate the LIMS 'Selected data file' row and toggle its visibility."""
        try:
            self._ensure_lims_controls()
            # self.save_db_btn = QPushButton("Save to Database", self.lims_based_widget)
            # self.save_db_btn.setObjectName("saveDbBtn")
            self.save_db_btn.setEnabled(False)  # starts inactive
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    def _reset_ui_for_new_dataset(self):
        # keep file paths & lims inputs so the user can quickly re-run
        self.reset_everything(keep_paths=True, keep_lims_inputs=True)

    def _active_iso(self) -> str | None:
        return getattr(self, "current_isotope", None)

    def _active_raw_df(self) -> pd.DataFrame | None:
        iso = self._active_iso()
        if iso and iso in self.multi_iso_raw and isinstance(self.multi_iso_rawiso, pd.DataFrame):
            return self.multi_iso_rawiso
        return getattr(self, "data", None)

    def _active_analysis_df(self) -> pd.DataFrame | None:
        iso = self._active_iso()
        if iso and iso in self.multi_iso_analysis and isinstance(self.multi_iso_analysisiso, pd.DataFrame):
            return self.multi_iso_analysisiso
        return getattr(self, "analysis_data", None)

    def _schedule_auto_plot(self, delay_ms: int = 80):
        try:
            self._auto_plot_timer.stop()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        self._auto_plot_timer.start(max(0, int(delay_ms)))

    def _auto_plot_safe(self):
        try:
            # optional: self.update_plot_configs()
            self.plot_on_canvas()
        except Exception as e:        
            logging.debug(f"Auto-plot skipped: {e}")

    def _dialog_start_dir(self, dir_key: str, *, fallback: str = "") -> str:
        """Return a good initial directory for file dialogs based on QSettings.
        Priority:
        1) settingsdir_key if exists and is a directory
        2) directory of settings['paths/last_data_file'] if that file exists
        3) provided fallback, else home directory
        """
        import os
        s = getattr(self, "settings", None)
        # 1) exact dir key
        if s is not None:
            try:
                d = s.value(dir_key, type=str)
                if d and os.path.isdir(d):
                    return d
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        # 2) last data file
        if s is not None:
            try:
                f = s.value("paths/last_data_file", type=str)
                if f and os.path.isfile(f):
                    d[2] = os.path.dirname(f)
                    if os.path.isdir(d[2]):
                        return d[2]
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        # 3) fallback or home
        if fallback:
            try:
                import os
                if os.path.isdir(fallback):
                    return fallback
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        try:
            import os
            return os.path.expanduser("~")
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return ""

    def _record_browse_result(self, path: str, *, dir_key: str, file_key: str | None = None):
        """Persist the chosen file's folder (and optionally the full file path)."""
        import os
        s = getattr(self, "settings", None)
        if s is None or not path:
            return
        try:
            d = os.path.dirname(path)
            if d:
                s.setValue(dir_key, d)
            if file_key:
                s.setValue(file_key, path)
            s.sync()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    def _stop_worker_thread_if_any(self):
        """Safely stop background worker (if running) to avoid stale signals."""
        try:
            th = getattr(self, "thread", None)
            wk = getattr(self, "worker", None)
            if wk is not None:
                try:
                    # if you added cancel flags in Worker, set them here
                    wk.include_ignored = False
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")
            if th is not None and th.isRunning():
                th.quit()
                th.wait(2000)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        finally:
            try:
                self.thread = None
                self.worker = None
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

    def _clear_tables_and_canvas(self):
        # tables
        try:
            if hasattr(self, "injection_table") and self.injection_table:
                self.injection_table.blockSignals(True)
                self.injection_table.clear()
                self.injection_table.setRowCount(0)
                self.injection_table.setColumnCount(0)
                self.injection_table.blockSignals(False)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try:
            if hasattr(self, "analysis_table") and self.analysis_table:
                self.analysis_table.blockSignals(True)
                self.analysis_table.clear()
                self.analysis_table.setRowCount(0)
                self.analysis_table.setColumnCount(0)
                self.analysis_table.blockSignals(False)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        # plot
        try:
            if hasattr(self, "figure") and self.figure:
                self.figure.clear()
            if hasattr(self, "canvas") and self.canvas:
                self.canvas.draw_idle()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    def reset_everything(self, *, keep_paths: bool = True, keep_lims_inputs: bool = True):
        """
        Canonical app reset used by all callers.
        - Stops worker & timers
        - Clears tables, plots, per-iso caches
        - Resets current_isotope, analysis view, plot menu
        - Keeps file paths & LIMS DSN/Run by default (so user can immediately reload)
        """
        # 1) stop async work
        try: self._auto_plot_timer.stop()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        self._stop_worker_thread_if_any()

        # 2) clear data & caches
        for attr in (
            "data", "injection_data", "analysis_data",
            "standards_data", "calibration_fits",
            "memory_fits", "drift_fits", "_analysis_full"
        ):
            try: setattr(self, attr, None)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        for attr in ("multi_iso_raw", "multi_iso_inj", "multi_iso_analysis", "multi_iso_fits", "multi_iso_post"):
            try: setattr(self, attr, {})
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

        # 3) analysis/iso state & plots
        self.current_isotope = None
        try:
            if hasattr(self, "active_iso_combo") and self.active_iso_combo:
                with QSignalBlocker(self.active_iso_combo):
                    self.active_iso_combo.clear()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        try:
            if hasattr(self, "plot_combo") and self.plot_combo:
                with QSignalBlocker(self.plot_combo):
                    self.plot_combo.clear()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        self._clear_tables_and_canvas()

        # 4) post-process tab (hide & clear)
        try:
            self.reset_postprocess(hide_tab=True)
            self._sync_post_tab_visibility()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # 5) inputs: keep file boxes/lims controls unless requested to clear
        if not keep_paths:
            try:
                self.data_file = None
                self.standards_file = None
                if hasattr(self, "data_file_edit"): self.data_file_edit.setText("")
                if hasattr(self, "standards_file_edit"): self.standards_file_edit.setText("")
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        if not keep_lims_inputs:
            try:
                if hasattr(self, "dsn_edit"): self.dsn_edit.setText("")
                if hasattr(self, "run_id_edit"): self.run_id_edit.setText("")
                self.si_analysis_run_id = None
                self.lims_loadlist = None
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

        # 6) Disable actions dependent on data
        try:
            if hasattr(self, "save_db_btn") and self.save_db_btn:
                self.save_db_btn.setEnabled(False)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # 7) set status & put UI in a safe default
        try: set_status(self.status_label,"Status: Ready.", "neutral")
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # Ensure “Load both” button state is re-evaluated
        try: self._update_load_both_enabled()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        
        if hasattr(self, "action_export_csv"):
            self.action_export_csv.setEnabled(False)

    # ---------------------------------------
    # (Optional) Convenience wrappers:
    # ---------------------------------------
    def browse_for_data_file(self, title: str = "Select Data File"):
        """Use this instead of calling QFileDialog directly (optional).
        It respects saved data_dir and records the result automatically.
        """
        start_dir = self._dialog_start_dir("paths/data_dir")
        path, _ = QFileDialog.getOpenFileName(
            self, title, start_dir, "Data (*.csv *.xlsx *.xls);;All Files (*)"
        )
        if path:
            self._record_browse_result(path, dir_key="paths/data_dir", file_key="paths/last_data_file")
        return path

    def browse_for_standards_file(self, title: str = "Select Standards File"):
        start_dir = self._dialog_start_dir("paths/standards_dir")
        path, _ = QFileDialog.getOpenFileName(
            self, title, start_dir, "Standards (*.csv *.json *.xlsx *.xls);;All Files (*)"
        )
        if path:
            self._record_browse_result(path, dir_key="paths/standards_dir", file_key="paths/standards_file")
        return path
    # === End of gui_settings_patch.py ===

    def _clear_layout(self, layout):
        try:
            while layout.count():
                item = layout.takeAt(0)
                w = item.widget()
                if w: w.deleteLater()
                else:
                    sub = item.layout()
                    if sub: self._clear_layout(sub)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    def export_standards_templates(self):
        # Ask user where to save
        dest_dir = QFileDialog.getExistingDirectory(self, "Choose destination for templates")
        if not dest_dir:
            return

        # --- Template contents (same as the downloadable ones) ---
        json_template = [
        {
            "sample_id": "USGS40",
            "ref_name": "USGS40",
            "roles": ["calibration", "control"],
            "values": [
            {"isotope": "d13C", "true": -26.389, "uncertainty": 0.05, "units": "permil", "scale": "VPDB"},
            {"isotope": "d15N", "true":  -4.520, "uncertainty": 0.10, "units": "permil", "scale": "AIR"}
            ]
        },
        {
            "sample_id": "SIL-Suc",
            "ref_name": "SIL Sucrose",
            "roles": ["calibration"],
            "values": [
            {"isotope": "d13C", "true": -11.85, "uncertainty": 0.05, "units": "permil", "scale": "VPDB"}
            ]
        },
        {
            "sample_id": "IAEA-N2",
            "ref_name": "IAEA N2",
            "roles": ["calibration","linearity"],
            "values": [
            {"isotope": "d15N", "true": 20.30, "uncertainty": 0.10, "units": "permil", "scale": "AIR"}
            ]
        },
        {
            "sample_id": "W-998",
            "ref_name": "W-998",
            "roles": ["linearity","memory"],
            "values": [
            {"isotope": "d18O", "true": -11.0, "uncertainty": 0.10, "units": "permil", "scale": "VSMOW"},
            {"isotope": "dD",   "true": -80.0, "uncertainty": 0.30, "units": "permil", "scale": "VSMOW"}
            ]
        },
        {
            "sample_id": "W-3",
            "ref_name": "W-3",
            "roles": ["blank"],
            "values": [
            {"isotope": "d18O", "true": 0.0, "units": "permil", "scale": "VSMOW"},
            {"isotope": "dD",   "true": 0.0, "units": "permil", "scale": "VSMOW"}
            ]
        }
        ]

        csv_long_header = "sample_id,ref_name,isotope,true,uncertainty,units,scale,roles\n"
        csv_long_rows = [
            "USGS40,USGS40,d13C,-26.389,0.05,permil,VPDB,calibration;control",
            "USGS40,USGS40,d15N,-4.520,0.10,permil,AIR,calibration;control",
            "SIL-Suc,SIL Sucrose,d13C,-11.85,0.05,permil,VPDB,calibration",
            "IAEA-N2,IAEA N2,d15N,20.30,0.10,permil,AIR,calibration;linearity",
            "W-998,W-998,d18O,-11.0,0.10,permil,VSMOW,linearity;memory",
            "W-998,W-998,dD,-80.0,0.30,permil,VSMOW,linearity;memory",
            "W-3,W-3,d18O,0.0,,permil,VSMOW,blank",
            "W-3,W-3,dD,0.0,,permil,VSMOW,blank",
        ]
        csv_wide_header = "sample_id,ref_name,d13C_true,d13C_uncertainty,d15N_true,d15N_uncertainty,d18O_true,d18O_uncertainty,dD_true,dD_uncertainty,roles\n"
        csv_wide_rows = [
            "USGS40,USGS40,-26.389,0.05,-4.520,0.10,,,,,calibration;control",
            "SIL-Suc,SIL Sucrose,-11.85,0.05,,,,,,,calibration",
            "W-998,W-998,,,,,-11.0,0.10,-80.0,0.30,linearity;memory"
        ]

        # Write files
        try:
            json_path = os.path.join(dest_dir, "standards_template.json")
            with open(json_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(json_template, indent=2))

            csv_long_path = os.path.join(dest_dir, "standards_template.csv")
            with open(csv_long_path, "w", encoding="utf-8", newline="") as f:
                f.write(csv_long_header + "\n".join(csv_long_rows) + "\n")

            csv_wide_path = os.path.join(dest_dir, "standards_template_wide.csv")
            with open(csv_wide_path, "w", encoding="utf-8", newline="") as f:
                f.write(csv_wide_header + "\n".join(csv_wide_rows) + "\n")

            QMessageBox.information(
                self, "Templates Saved",
                f"Exported:\n{json_path}\n{csv_long_path}\n{csv_wide_path}"
            )
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))

    def _ensure_active_isotope_selector(self):
        """Create the 'Active isotope' row once and alias the combo as both iso_combo and active_iso_combo."""
        
        if hasattr(self, "active_iso_row_widget") and self.active_iso_row_widget:
            return

        self.active_iso_row_widget = QWidget(self.content_panel)
        row = QHBoxLayout(self.active_iso_row_widget)
        row.setContentsMargins(0, 10, 0, 0)

        label = QLabel("Active isotope:", self.active_iso_row_widget)

        # Create the combo ONCE and expose it under BOTH names
        combo = QComboBox(self.active_iso_row_widget)
        combo.setObjectName("iso_combo")

        # Hook the change signal once
        combo.currentTextChanged.connect(lambda _=None: self._schedule_auto_plot())
        try:
            combo.currentTextChanged.connect(self.on_active_isotope_changed)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        
        # Alias so all code paths see the same widget
        self.iso_combo = combo
        self.active_iso_combo = combo
        # (re)[wire] the signal safely (avoid duplicate connections)
        try:
            self.active_iso_combo.currentTextChanged.disconnect()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        self.active_iso_combo.currentTextChanged.connect(
            lambda text: self._set_active_isotope(text, source="combo")
        )        
        # Use a single combo everywhere:
        self.isotope_combo = self.active_iso_combo
        
        # ... (combo creation) ...
        combo.setFixedWidth(250)
        row.addStretch()  # pushes everything to the right      
        row.addWidget(label)
        row.addWidget(combo)
        
        try:
            self.content_layout.addWidget(self.active_iso_row_widget)
        except Exception as e:
            # Fallback just in case
            logging.error(f"Failed to add isotope selector to content_layout: {e}")
            try:
                self.input_layout.addWidget(self.active_iso_row_widget)
            except Exception as e:

                logging.warning(f"Exception caught: {e}") 

        self.active_iso_row_widget.setVisible(False)
        
    def _set_combo_items(self, combo, items):
        if combo is None:
            return
        try:
            was = combo.blockSignals(True)
            combo.clear()
            combo.addItems(items or [])
            combo.blockSignals(was)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    def _populate_isotope_selector_from(self, df=None, *, candidates=None, preserve=True):
        """
        Populate the Active Isotope combo.
        - If `candidates` is provided, use it (don’t infer from df).
        - If `preserve` and the set of items is unchanged, do not rebuild.
        """

        self._ensure_active_isotope_selector()
        combo = self.active_iso_combo
        roww  = self.active_iso_row_widget

        # Decide the items to show
        if candidates is not None:
            items = [str(x) for x in candidates if x]
            # dedupe while preserving order
            seen = set(); items = [x for x in items if not (x in seen or seen.add(x))]
        else:
            # infer from df columns
            if not isinstance(df, pd.DataFrame) or df.empty:
                items = []
            else:
                cols = {str(c) for c in df.columns}
                bases = ("d18O", "dD", "d17O", "d13C", "d15N")
                items = [b for b in bases if any(c == b or c.startswith(b + "_") for c in cols)]
                if "isotope" in cols:
                    try:
                        more = (df["isotope"].astype(str).str.strip()
                                .str.replace("δ", "d")
                                .unique().tolist())
                        for m in more:
                            if m and m not in items:
                                items.append(m)
                    except Exception as e:

                        logging.warning(f"Exception caught: {e}")

        # Nothing to show
        if not items:
            combo.blockSignals(True); combo.clear(); combo.blockSignals(False)
            roww.setVisible(False)
            self.current_isotope = None
            return

        # Preserve: don’t rebuild if we already have the same set
        current_items = [combo.itemText(i) for i in range(combo.count())]
        if preserve and set(current_items) == set(items):
            # just ensure selection is valid
            want = self.current_isotope if self.current_isotope in items else items[0]
            if combo.currentText() != want:
                combo.blockSignals(True); combo.setCurrentText(want); combo.blockSignals(False)
            roww.setVisible(True); combo.setVisible(True)
            return

        # Rebuild
        want = self.current_isotope if self.current_isotope in items else (combo.currentText() if combo.currentText() in items else items[0])
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(items)
        combo.setCurrentText(want)
        combo.blockSignals(False)

        self.current_isotope = combo.currentText()
        roww.setVisible(True); combo.setVisible(True)



    def _remember_plot_selection(self) -> str:
        try:
            return self.plot_combo.currentText()
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return ""

    def _restore_plot_selection(self, prev_name: str):
        try:
            combo = self.plot_combo
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return
        if combo is None:
            return
        was = combo.blockSignals(True)

        # 1) Exact match
        idx = combo.findText(prev_name)
        if idx != -1:
            combo.setCurrentIndex(idx)
            combo.blockSignals(was)
            return

        # 2) Fallback by family keyword (keeps user intent, e.g. Drift vs Memory)
        families = ("Drift", "Memory", "Combined", "Calibration", "Time Series", "δ18O vs δD", "d18O vs dD")
        chosen = False
        for fam in families:
            if prev_name.startswith(fam):
                for i in range(combo.count()):
                    if combo.itemText(i).startswith(fam):
                        combo.setCurrentIndex(i)
                        chosen = True
                        break
                break

        # 3) If nothing matched, keep whatever current index is (don’t force first)
        combo.blockSignals(was)

    def _set_active_isotope(self, iso: str, *, source: str = ""):
        if not iso:
            return
        self.current_isotope = iso

        # Sync the Input-panel combo (active_iso_combo)
        try:
            if hasattr(self, "active_iso_combo") and self.active_iso_combo.currentText() != iso:
                with QSignalBlocker(self.active_iso_combo):
                    self.active_iso_combo.setCurrentText(iso)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # If you still have a plot-tab isotope combo (iso_combo), keep it in sync too
        try:
            if hasattr(self, "iso_combo") and self.iso_combo.currentText() != iso:
                with QSignalBlocker(self.iso_combo):
                    self.iso_combo.setCurrentText(iso)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # Refresh data tables to this isotope
        try:
            self._refresh_tables_for_active_isotope()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # Refresh Post-processing view to this isotope
        try:
            self._update_postprocess_view()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # Rebuild plot list (so any per-iso plots toggle correctly)
        try:
            self.update_plot_configs()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # If you wired auto-plot, trigger it (non-destructive)
        try:
            self._schedule_auto_plot(0)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        self._refresh_post_for_active_iso(False)
       
    def on_active_isotope_changed(self, iso: str):
        """Active Isotope change:
        - EA: keep your current behavior (peak handling elsewhere).
        - DI: swap cached frames if available.
        - LASERS (Picarro/LGR): preserve RAW (full) and derive Analysis for the selected iso.
        """
        
        if not iso:
            return

        self.current_isotope = iso
        label = (self.instrument_combo.currentText() or "").strip().lower()

        is_ea = ("ea" in label) or ("irms" in label) or ("thermo" in label and "di" not in label)
        is_di = ("di" in label) and not is_ea
        is_laser = ("picarro" in label) or ("lgr" in label) or ("laser" in label)

        # ---------- EA ----------
        if is_ea:
            try:
                if hasattr(self, "_ea_apply_config_peak") and callable(self._ea_apply_config_peak):
                    self._ea_apply_config_peak(iso)
                    return
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            try: self.update_data_table(self.injection_table, self.injection_data)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            try: self.update_data_table(self.analysis_table, self.analysis_data)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            try: self.update_plot_configs()
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            return

        # ---------- DI ----------
        if is_di:
            inj_map = getattr(self, "multi_iso_inj", {}) or {}
            ana_map = getattr(self, "multi_iso_analysis", {}) or {}
            inj = inj_map.get(iso)
            ana = ana_map.get(iso)

            # Only replace RAW if we have a real per-iso inj frame
            if isinstance(inj, pd.DataFrame) and not inj.empty:
                self.injection_data = inj.copy()
                self.data = self.injection_data

            # Analysis uses cached per-iso if available, else mirrors RAW
            if isinstance(ana, pd.DataFrame) and not ana.empty:
                self.analysis_data = ana.copy()
            else:
                self.analysis_data = getattr(self, "injection_data", None)

            try: self.update_data_table(self.injection_table, self.injection_data)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            try: self.update_data_table(self.analysis_table, self.analysis_data)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            try: self.update_plot_configs()
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            return

        # ---------- LASERS ----------
        if is_laser:
            # Tables unchanged; only plots should respond to the iso toggle.
            try:
                self.update_plot_configs()
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            return
    
        # ---------- Fallback (unknown instrument) ----------
        inj_map = getattr(self, "multi_iso_inj", {}) or {}
        ana_map = getattr(self, "multi_iso_analysis", {}) or {}
        inj = inj_map.get(iso)
        ana = ana_map.get(iso)

        if isinstance(inj, pd.DataFrame) and not inj.empty:
            self.injection_data = inj.copy()
            self.data = self.injection_data  # keep raw visible
        # else: leave RAW as-is

        if isinstance(ana, pd.DataFrame) and not ana.empty:
            self.analysis_data = ana.copy()
        else:
            self.analysis_data = getattr(self, "injection_data", None)

        try: self.update_data_table(self.injection_table, self.injection_data)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try: self.update_data_table(self.analysis_table, self.analysis_data)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try: self.update_plot_configs()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    def get_active_isotope(self) -> str | None:
        try:
            iso = self.active_iso_combo.currentText().strip()
            return iso if iso else None
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return getattr(self, "current_isotope", None)

    def _toggle_input_panel(self):
        """
        Animates the input_panel (settings) in or out.
        """
        self.panel_animation.stop()
        
        if self.is_panel_collapsed:
            # --- EXPAND ---
            self.is_panel_collapsed = False
            self.input_panel.setVisible(True) # Show it *before* animating
            
            self.toggle_sidebar_button.setProperty("collapsed", False)
            self._style_icon_bar_widgets()
            
            target_width = self.SIDEBAR_WIDTH_EXPANDED
            self.panel_animation.setStartValue(0)
            self.panel_animation.setEndValue(target_width)
            self.panel_animation.start()
            
        else:
            # --- COLLAPSE ---
            self.is_panel_collapsed = True
            
            self.toggle_sidebar_button.setProperty("collapsed", True)
            self._style_icon_bar_widgets()

            target_width = 0
            self.panel_animation.setStartValue(self.input_panel.width())
            self.panel_animation.setEndValue(target_width)
            self.panel_animation.start()
            
    def _style_icon_bar_widgets(self):
        """Forces a stylesheet refresh on the icon bar widgets."""
        widgets = [
            self.icon_bar, 
            self.toggle_sidebar_button
        ]
        for w in widgets:
            w.style().unpolish(w)
            w.style().polish(w)

    def _on_panel_animation_finished(self):
        """
        Called when the expand/collapse animation is complete.
        """
        if self.is_panel_collapsed:
            self.input_panel.setVisible(False) # Hide it *after* animating
            self.input_panel.setMaximumWidth(0)
        else:
            self.input_panel.setMaximumWidth(self.SIDEBAR_WIDTH_EXPANDED)

    def _set_input_panel_collapsed(self, collapse: bool):
        """
        Sets the input panel to a specific collapsed or expanded state.
        (This is a "setter" and not a "toggle").
        """
        if self.is_panel_collapsed == collapse:
            return  # Already in the desired state, do nothing
        
        self._toggle_input_panel()
 
    def _apply_sidebar_dark_theme(self, enable: bool = True):
        panel = getattr(self, "input_panel", None)
        if not panel:
            return

        if not enable:
            panel.setStyleSheet("")  # clear
            return

        # Apply to panel; selectors hit the body widget by objectName
        panel.setStyleSheet("""
        /* BODY backgrounds — these two ensure the whole panel is dark */
        #InputDockBody {
            background-color: #0B1220;  /* deep slate/navy */
        }
        #InputPanelTitle {
            background-color: #1F2937; /* slate-800 */
            color: #E5E7EB; /* Light text */
            font-weight: bold;
            font-size: 12px;
            padding: 3px;
            border-radius: 4px;
        }
        /* Text colors inside the sidebar */
        #InputDockBody QLabel,
        #InputDockBody QCheckBox,
        #InputDockBody QRadioButton,
        #InputDockBody QGroupBox {
            color: #E5E7EB;
            font-size: 11px;
        }
        #InputDockBody QComboBox,
        #InputDockBody QLineEdit,
        #InputDockBody QPushButton {
            font-size: 11px;
        }

        /* GroupBox styling */
        #InputDockBody QGroupBox {
            border: 1px solid #1F2937;   /* slate-800 */
            margin-top: 4px;
            border-radius: 4px;
        }
        #InputDockBody QGroupBox::title {
            subcontrol-origin: margin;
            left: 6px;
            padding: 0 3px;
            color: #E5E7EB;
            background: transparent;
        }

        /* Inputs */
        #InputDockBody QLineEdit,
        #InputDockBody QComboBox,
        #InputDockBody QTextEdit,
        #InputDockBody QSpinBox,
        #InputDockBody QDoubleSpinBox {
            background-color: #111827;         /* slate-900 */
            color: #E5E7EB;
            border: 1px solid #374151;         /* slate-700 */
            border-radius: 4px;
            padding: 2px 4px;
            max-height: 24px;
        }
        #InputDockBody QComboBox::drop-down { border: none; }

        /* Buttons */
        #InputDockBody QPushButton {
            background-color: #1F2937;         /* slate-800 */
            color: #E5E7EB;
            border: 1px solid #374151;
            padding: 3px 6px;
            border-radius: 5px;
            max-height: 26px;
        }
        #InputDockBody QPushButton:hover  { background-color: #273043; }
        #InputDockBody QPushButton:pressed{ background-color: #2F3A52; }

        /* Style the main "Process Data" button differently */
        #InputDockBody QPushButton[objectName="processButton"] {
            background-color: #ef6c00;  /* orange 800 */
            color: #fff;
            font-weight: 600;
            padding: 4px 10px;
            border: none;
            border-radius: 5px;
            max-height: 28px;
        }
        #InputDockBody QPushButton[objectName="processButton"]:hover:!disabled { background-color: #e65100; }
        #InputDockBody QPushButton[objectName="processButton"]:disabled {
            background-color: #9e9e9e;
            color: #f0f0f0;
        }
        """)

    def on_actions_menu_requested(self):
        """
        Shows a pop-up menu containing data-dependent actions.
        """
        menu = QMenu(self)
        menu.addAction(self.action_export_csv)
        menu.addAction(self.action_export_templates)
        
        # Show the menu below the button
        button = self.sender()
        menu_pos = button.mapToGlobal(QPoint(0, button.height()))
        menu.exec_(menu_pos)

    # --- EA raw/analysis guards ------------------------------------------------
    def _update_load_both_enabled(self):
        """Enable 'Load Both' only when both paths are non-empty and exist."""
        import os
        btn = getattr(self, "load_both_btn", None)
        if not btn:
            return
        data_path = self.data_file_edit.text().strip() if hasattr(self, "data_file_edit") else ""
        std_path  = self.standards_file_edit.text().strip() if hasattr(self, "standards_file_edit") else ""
        
        ok = bool(data_path and os.path.exists(data_path)) and (not std_path or os.path.exists(std_path))
        btn.setEnabled(ok)

    def _load_data_and_standards_from_textboxes(self):
        """Load Data + Standards using the text in their line-edits (no dialogs)."""
        import os
        data_path = self.data_file_edit.text().strip() if hasattr(self, "data_file_edit") else ""
        std_path  = self.standards_file_edit.text().strip() if hasattr(self, "standards_file_edit") else ""

        if not data_path or not os.path.exists(data_path):
            QMessageBox.warning(self, "Missing Data File", "Please choose a valid Data file.")
            return

        # Load Data first (this sets up Raw/Analysis and current sample_id universe)
        self.select_data_file(path=data_path, show_dialog=False)

        # Standards are optional; only load if provided
        if std_path:
            if os.path.exists(std_path):
                self.select_standards_file(path=std_path, show_dialog=False)
            else:
                QMessageBox.warning(self, "Missing Standards File", "Standards path was set but the file does not exist.")
      
    def _options_for_instrument(self, instr: str) -> list:
        """Which 'Option' choices we show for a given instrument."""
        if instr in ("LGR", "Picarro"):
            return ["File-Based", "LIMS-Based"]
        if instr in ("IRMS (Thermo DI)", "IRMS (EA)"):
            return ["File-Based"]
        return ["File-Based"]

    def _rebuild_option_items(self):
        """Always offer File-Based and LIMS-Based, regardless of instrument."""
        items = ["File-Based", "LIMS-Based"]
        self.option_combo.blockSignals(True)
        prev = self.option_combo.currentText() if self.option_combo.count() else "File-Based"
        self.option_combo.clear()
        self.option_combo.addItems(items)
        if prev in items:
            self.option_combo.setCurrentText(prev)
        else:
            self.option_combo.setCurrentIndex(0)
        self.option_combo.blockSignals(False)
    
    def on_option_changed(self, _label: str):
        self._refresh_input_panel()


    # put this inside class MainWindow
    def _get_or_create_output_tabs(self):
        """
        Find an existing QTabWidget to host output tabs, or create one and
        install it into the central widget layout.
        """
        # 1) Common attribute names developers use
        for attr in ("tabs", "output_tabs", "tab_widget", "central_tabs"):
            tabw = getattr(self, attr, None)
            try:
                from PyQt5.QtWidgets import QTabWidget
                if isinstance(tabw, QTabWidget):
                    return tabw
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

        # 2) Look up any QTabWidget already in the widget tree
        try:
            from PyQt5.QtWidgets import QTabWidget
            found = self.findChildren(QTabWidget)
            if found:
                return found[0]
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # 3) Create one and place it into the central widget
        from PyQt5.QtWidgets import QWidget, QVBoxLayout, QTabWidget
        cw = self.centralWidget()
        if cw is None:
            cw = QWidget(self)
            self.setCentralWidget(cw)

        lay = cw.layout()
        if lay is None:
            lay = QVBoxLayout(cw)
            cw.setLayout(lay)

        tabw = QTabWidget(cw)
        lay.addWidget(tabw)
        # expose it for future callers
        self.tabs = tabw
        return tabw

    def _ensure_isotope_controls(self):
        """Create the 'Isotopes to Process' checkbox group for lasers if missing."""
        from PyQt5.QtWidgets import QGroupBox, QHBoxLayout, QCheckBox
        if hasattr(self, "isotope_group") and self.isotope_group:
            return

        self.isotope_group = QGroupBox("Isotopes to Process", self.input_panel)
        self.isotope_group_layout = QHBoxLayout(self.isotope_group)

        if not hasattr(self, "d18O_check"): self.d18O_check = QCheckBox("δ18O")
        if not hasattr(self, "dD_check"):   self.dD_check   = QCheckBox("δD")
        if not hasattr(self, "d17O_check"): self.d17O_check = QCheckBox("δ17O")

        self.d18O_check.setChecked(True)
        self.dD_check.setChecked(True)
        self.d17O_check.setChecked(False)

        for w in (self.d18O_check, self.dD_check, self.d17O_check):
            self.isotope_group_layout.addWidget(w)

        self.isotope_group.setVisible(False)  # show after laser data loads
        self.input_layout.addWidget(self.isotope_group)


    def _selected_isotopes_for_laser(self):
        """Return isotopes to process, honoring checkboxes when visible; else infer from data."""
        
        df = getattr(self, "data", None)
        cols = set(df.columns) if isinstance(df, pd.DataFrame) else set()
        avail = [iso for iso in ("d18O", "dD", "d17O")
                if iso in cols or f"{iso}_calibrated" in cols]

        picks = []
        for name, iso in (("d18O_check", "d18O"), ("dD_check", "dD"), ("d17O_check", "d17O")):
            w = getattr(self, name, None)
            if w is not None and w.isVisible():
                if w.isChecked() and iso in avail:
                    picks.append(iso)

        return picks if picks else avail

    def _refresh_input_panel(self):
        
        instr = self.instrument_combo.currentText() if hasattr(self, "instrument_combo") else ""
        opt   = self.option_combo.currentText() if hasattr(self, "option_combo") else "File-Based"
        df    = getattr(self, "data", None)
        data_loaded = isinstance(df, pd.DataFrame) and not df.empty

        is_laser = instr in ("LGR", "Picarro")
        is_ea    = instr == "IRMS (EA)"
        is_di    = instr == "IRMS (Thermo DI)"
        is_lims  = (opt == "LIMS-Based")   # ← now allowed for all instruments

        if hasattr(self, "save_db_btn"):
            self.save_db_btn.setVisible(is_lims)
            
        # Containers: show either Files or LIMS controls
        if hasattr(self, "files_group"):
            self.files_group.setVisible(not is_lims)
        if hasattr(self, "lims_based_widget"):
            self.lims_based_widget.setVisible(is_lims)

        # Standards row (file picker):
        # - File-Based: visible for all instruments (EA label says optional)
        # - LIMS-Based: hidden (standards are fetched from LIMS)
        if hasattr(self, "standards_row_widget"):
            self.standards_row_widget.setVisible(not is_lims)

        if hasattr(self, "standards_label"):
            if is_ea and not is_lims:
                self.standards_label.setText("Standards (optional, used for calibration):")
            else:
                self.standards_label.setText("Standards:")

        # Laser-only isotope checkboxes appear after data load (file or LIMS)
        if hasattr(self, "isotope_group"):
            self.isotope_group.setVisible(is_laser and data_loaded)
        # include_ignored_check visibility is controlled by pp_laser_group.setVisible(is_laser)
        # Active isotope row: visible if we have data and at least one isotope candidate
        try:
            self._populate_isotope_selector_from(df if data_loaded else pd.DataFrame())
            if hasattr(self, "active_iso_row_widget") and hasattr(self, "iso_combo"):
                self.active_iso_row_widget.setVisible(data_loaded and self.iso_combo.count() > 0)
        except Exception:
            if hasattr(self, "active_iso_row_widget"):
                self.active_iso_row_widget.setVisible(False)

        # Post-processing groups
        if hasattr(self, "pp_laser_group"):
            self.pp_laser_group.setVisible(is_laser)
        if hasattr(self, "pp_irms_group"):
            # Show for EA and DI (EA controls meaningful; DI can be extended later)
            self.pp_irms_group.setVisible(is_ea or is_di)

        # Process button readiness:
        # - LIMS-Based: once data is loaded from LIMS, you're ready (all instruments)
        # - File-Based Laser: require standards file
        # - File-Based EA/DI: data is enough; standards are optional (if present -> calibrate)
        ready = False
        if is_lims:
            ready = data_loaded
        else:
            if is_laser:
                std_df = getattr(self, "standards_data", None)
                ready = data_loaded and isinstance(std_df, pd.DataFrame) and not std_df.empty
            elif is_ea or is_di:
                ready = data_loaded

        if hasattr(self, "process_button"):
            self.process_button.setEnabled(bool(ready))

        is_ea = self.instrument_combo.currentText() == "IRMS (EA)"
        if hasattr(self, "ea_peak_group"):
            self.ea_peak_group.setVisible(is_ea and getattr(self, "data", None) is not None)

    def _show_active_isotope_selector(self, show: bool):
        if hasattr(self, "active_iso_row_widget") and self.active_iso_row_widget:
            self.active_iso_row_widget.setVisible(bool(show))

    def on_instrument_changed(self, instrument_text, format_id=None):
        self._reset_ui_for_new_dataset()
        self._rebuild_option_items()
        self._refresh_input_panel()
        # After select_data_file() loads the raw EA/DI file:
        self._ensure_post_ui()
        self.reset_postprocess(hide_tab=True)
        self._sync_post_tab_visibility()

        # After restoring from QSettings:
        try:
            d = self.settings.value("paths/last_data_file", "", type=str) or ""
            s = self.settings.value("paths/last_standards_file", "", type=str) or ""
            if hasattr(self, "data_file_edit"):      self.data_file_edit.setText(d)
            if hasattr(self, "standards_file_edit"): self.standards_file_edit.setText(s)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # <-- Put these two lines right here -->
        self._update_load_both_enabled()

        QTimer.singleShot(0, self._update_load_both_enabled)

        # clear diagnostics/fits
        self.memory_fits = {}; self.drift_fits = {}; self.calibration_fits = {}
        # clear plot picker
        try:
            self.plot_configs = {}; self.plot_configs_mpl = {}; self.plot_combo.clear()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        # clear figure
        try:
            if hasattr(self, "figure") and self.figure is not None: self.figure.clf()
            if hasattr(self, "canvas") and self.canvas is not None: self.canvas.draw_idle()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        # isotope toggles visibility
        try:
            if instrument_text in ("IRMS (EA)", "IRMS (Thermo DI)"):
                if hasattr(self, "isotope_group"): self.isotope_group.setVisible(False)
            else:
                if hasattr(self, "isotope_group"): self.isotope_group.setVisible(True)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        # disable plot tab until re-populated
        try:
            idx = self.output_tabs.indexOf(self.plot_tab)
            if idx != -1: self.output_tabs.setTabEnabled(idx, False)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        # keep Post tab visibility consistent
        try:
            self._sync_post_tab_visibility()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        # Laser instruments show the Active Isotope row; IRMS hide it
        self._show_active_isotope_selector(instrument_text in ("LGR", "Picarro"))

        # Auto-load protocol for this instrument
        try:
            if hasattr(db_manager, '_engine') and db_manager._engine is not None:
                protocol = ProtocolManager.get_default_protocol(module='SIAM', file_format_id=format_id)
                # print(f"file format {format_id}, instrument_text {instrument_text}, protocol_instrument {protocol.settings.get('instrument_name', '')} ")
                if protocol:
                    # Check if protocol matches THIS instrument (exact match!)
                    protocol_instrument = protocol.settings.get('instrument_name', '')
                    
                    if protocol_instrument == instrument_text:
                        self.current_protocol = protocol
                        self._apply_protocol_settings(protocol)                        
                        logging.info(f"Auto-loaded protocol for {instrument_text}: {protocol.name}")
                    else:
                        logging.info(f"Default protocol is for {protocol_instrument}, not {instrument_text}")
                else:
                    logging.info(f"No default SIAM protocol found")
        except Exception as e:
            logging.warning(f"Could not load protocol on instrument change: {e}")
        
                
    def _detect_isotopes_in_df(self, df: pd.DataFrame):
        if df is None or getattr(df, "empty", True):
            return []
        cols = set(map(str, df.columns))
        known = ["d18O", "dD", "d17O", "d13C", "d15N"]
        found = [iso for iso in known if iso in cols]
        if "isotope" in cols:
            try:
                vals = df["isotope"].astype(str).str.strip().str.replace("δ", "d")
                for v in vals.unique().tolist():
                    if v and v not in found:
                        found.append(v)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        order = ["d18O", "dD", "d17O", "d13C", "d15N"]
        return [k for k in order if k in found] + [k for k in found if k not in order]
                
    def _parse_peak_combo(self, combo) -> int | None:
        txt = (combo.currentText() or "").strip()
        m = re.match(r"\s*(\d+)", txt)
        return int(m.group(1)) if m else None
                    
    def update_workflow_ui(self, selection):
        if selection == "File-Based":
            self.file_based_widget.show()
            self.lims_based_widget.hide()
        else:
            self.file_based_widget.hide()
            self.lims_based_widget.show()
    def _on_output_tab_changed(self, idx):
        try:
            if self.output_tabs.widget(idx) is self.plot_tab:
                self._schedule_auto_plot(0)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
            
    def _peek_columns(self, file_path: str):
        """Return lowercase, stripped column names from the first sheet/line."""
        
        ext = os.path.splitext(file_path)[1].lower()
        cols = []

        if ext in (".csv", ".txt"):
            trials = [
                dict(sep=",", encoding="utf-8"),
                dict(sep=",", encoding="latin1"),
                dict(sep=";", encoding="utf-8"),
                dict(sep=";", encoding="latin1"),
            ]
            for kw in trials:
                try:
                    hdr = pd.read_csv(file_path, nrows=0, **kw).columns
                    cols = [str(c).strip().lower() for c in hdr]
                    if cols:
                        break
                except Exception as e:

                    logging.warning(f"Exception caught: {e}"); continue
        elif ext in (".xls", ".xlsx"):
            try:
                hdr = pd.read_excel(file_path, nrows=0).columns
                cols = [str(c).strip().lower() for c in hdr]
            except Exception:
                cols = []
        return cols

    def detect_instrument_from_file(self, file_path) -> str:
        """
        Detect instrument from header/extension. Returns one of:
        'LGR', 'Picarro', 'IRMS (Thermo DI)', 'IRMS (EA)', or ''.
        **Does not** use 'Gasconfiguration' + 'Identifier 1' as the decider.
        """
        import os, logging
        cols = self._peek_columns(file_path)
        ext = os.path.splitext(file_path)[1].lower()

        # helpers
        def has_any(tokens):
            return sum(any(tok in c for c in cols) for tok in tokens)

        # canonical cues (work on lower-cased headers)
        lgr_tokens      = ["delta 18o/16o", "delta d/h", "h2o conc", "injection no", "date-time analyzed"]
        picarro_tokens  = ["d(18_16)mean", "d(d_h)mean", "h2o_mean", "inj nr", "time code"]
        ea_tokens       = ["d13c", "13c/12c", "d 13c/12c", "d15n", "15n/14n", "d 15n/14n"]
        di_tokens       = ["dual inlet", "dual inlet ref", "time code", "d 18o/16o", "d 2h/1h", "d18o", "d2h", "dd/h", "analysis"]

        scores = {
            "LGR": has_any(lgr_tokens),
            "Picarro": has_any(picarro_tokens),
            "IRMS (EA)": has_any(ea_tokens) * 3,  # strong weight for C/N
            "IRMS (Thermo DI)": has_any(di_tokens),
        }

        # Extension bias: Excel often DI (multi-sheet), but not decisive
        if ext in (".xls", ".xlsx"):
            scores["IRMS (Thermo DI)"] += 1

        # pick best
        label = max(scores, key=scores.get) if any(scores.values()) else ""

        # tie-breaker EA vs DI if both >0 and equal
        if label in ("IRMS (EA)", "IRMS (Thermo DI)"):
            if scores["IRMS (EA)"] == scores["IRMS (Thermo DI)"] and scores["IRMS (EA)"] > 0:
                # Prefer EA if C/N tokens present; otherwise DI if water tokens present
                if has_any(ea_tokens) > 0:
                    label = "IRMS (EA)"
                elif has_any(["d18o", "d2h", "d 18o/16o", "d 2h/1h"]) > 0:
                    label = "IRMS (Thermo DI)"
                elif ext in (".xls", ".xlsx"):
                    label = "IRMS (Thermo DI)"

        if label:
            self._set_instrument_combo_silent(label)
            logging.info(f"Detected instrument: {label}")
        else:
            logging.info("Instrument detection: no match (leaving user selection unchanged)")
        return label

    def _show_raw_table(self):
        """Always show the full, unfiltered dataframe on the Raw/Injection tab."""
        try:
            df = getattr(self, "data", None)
            self.update_data_table(self.injection_table, df if df is not None else pd.DataFrame())
            # if you use the frozen row header trick:
            try: self._freeze_sample_id(self.injection_table, df)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        except Exception as e:
            logging.debug(f"_show_raw_table skipped: {e}")

    def _reset_ui_for_new_dataset(self):
        # wipe frames
        self.data = None
        self.injection_data = None
        self.analysis_data = None
        self.ea_raw_df = None
        self.memory_fits = {}
        self.drift_fits = {}
        self.calibration_fits = {}
        self.post_results = None
        self.qc_summary = None
        self.batch_summary = None

        # wipe tables
        for t in [getattr(self, "injection_table", None),
                getattr(self, "analysis_table", None),
                getattr(self, "standards_table", None)]:
            if t is None: continue
            try:
                t.blockSignals(True)
                t.clear()
                t.setRowCount(0); t.setColumnCount(0)
                t.blockSignals(False)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

        # wipe plot ui
        try:
            if hasattr(self, "figure") and self.figure is not None:
                self.figure.clf()
            if hasattr(self, "canvas") and self.canvas is not None:
                self.canvas.draw_idle()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try:
            if hasattr(self, "plot_combo"):
                self.plot_combo.clear()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # isotope selector -> hidden until we know the active isotope
        try:
            if hasattr(self, "active_iso_combo"):
                self.active_iso_combo.blockSignals(True)
                self.active_iso_combo.clear()
                self.active_iso_combo.blockSignals(False)
            if hasattr(self, "active_iso_row_widget"):
                self.active_iso_row_widget.setVisible(False)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    def _add_sample_id_from_vendor(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Ensure a clean string 'sample_id' column exists.
        Primary rule (per your spec):
        sample_id = first token of 'Identifier 1' split by '/'
        Robust fallbacks: 'Identifier1', 'Sample ID', 'Sample Name', 'Name', 'ID', 'Ref Name'.
        If none exist, synthesize from 'Line' or 'Analysis', else row index.
        Also fixes Excel-floaty names like '123.0' -> '123'.
        """
        if not isinstance(df, pd.DataFrame) or df.empty:
            return df
        out = df.copy()
        cols = list(out.columns)

        # Prefer 'Identifier 1'
        idcol = (_canon_lookup(cols, "Identifier 1") or
                _canon_lookup(cols, "Identifier1") or
                _canon_lookup(cols, "Sample ID") or
                _canon_lookup(cols, "SampleID") or
                _canon_lookup(cols, "Sample Name") or
                _canon_lookup(cols, "SampleName") or
                _canon_lookup(cols, "Name") or
                _canon_lookup(cols, "ID") or
                _canon_lookup(cols, "Ref Name") or
                _canon_lookup(cols, "ref_name"))

        if idcol:
            ser = out[idcol].astype(str).str.strip()
            # take first chunk before '/'
            ser = ser.str.split("/", n=1, expand=False).str[0].fillna("")
            # collapse whitespace
            ser = ser.str.replace(r"\s+", " ", regex=True).str.strip()
            # fix integer-looking floats "123.0" -> "123"
            def _defloat(s):
                s = s or ""
                return str(int(float(s))) if re.fullmatch(r"\d+(\.0+)?", s) else s
            ser = ser.map(_defloat)
            out["sample_id"] = ser.astype(str).str.strip()
            return out

        # Fallbacks: Line or Analysis
        linecol = _canon_lookup(cols, "Line")
        anacol = _canon_lookup(cols, "Analysis")
        if linecol:
            out["sample_id"] = pd.to_numeric(out[linecol], errors="coerce").apply(
                lambda v: f"L{int(v)}" if pd.notna(v) else ""
            )
        elif anacol:
            out["sample_id"] = pd.to_numeric(out[anacol], errors="coerce").apply(
                lambda v: f"A{int(v)}" if pd.notna(v) else ""
            )
        else:
            out["sample_id"] = out.index.astype(str)

        out["sample_id"] = out["sample_id"].astype(str).str.strip()
        return out

    def _on_di_active_iso_changed(self, iso: str) -> None:
        """Switch DI raw/analysis views when Active Isotope changes."""
        try:
            if not iso:
                return
            self.current_isotope = iso
            # swap raw
            if isinstance(getattr(self, "di_raw_by_iso", None), dict) and iso in self.di_raw_by_iso:
                self.data = self.di_raw_by_isoiso.copy()
            else:
                self.data = pd.DataFrame()

            # swap analysis if already processed for this iso
            ana = pd.DataFrame()
            if isinstance(getattr(self, "di_analysis_by_iso", None), dict) and iso in self.di_analysis_by_iso:
                ana = self.di_analysis_by_isoiso.copy()

            self.update_data_table(self.injection_table, self.data)
            self.update_data_table(self.analysis_table, ana)

            # plots depend on what's currently visible
            self.update_plot_configs()
            if hasattr(self, "_schedule_auto_plot"):
                self._schedule_auto_plot(0)
            self._refresh_post_for_active_iso(False)
        except Exception as e:
            logging.debug(f"DI active isotope switch failed: {e}")
    
    def _load_di_multisheet(self, path: str) -> dict[str, pd.DataFrame]:
        """
        Robust DI loader:
        - Works for XLS/XLSX (multi-sheet) and CSV (single-sheet).
        - Always creates 'sample_id' and 'timestamp'.
        - Canonicalizes isotope columns from vendor headers like 'd 18O/16O  Mean' -> 'd18O'.
        - Never returns an empty dict (defaults to d18O if in doubt).
        """
        import os, re, numpy as np, pandas as pd

        # ---- read workbook or csv ----
        def _read_any(p):
            ext = os.path.splitext(p)[1].lower()
            if ext in (".xlsx", ".xls"):
                try:
                    return pd.read_excel(p, sheet_name=None)
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")
            # CSV fallback(s)
            try:
                return {"Sheet1": pd.read_csv(p)}
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); return {"Sheet1": pd.read_csv(p, sep=";")}

        sheets = _read_any(path) or {}
        out: dict[str, pd.DataFrame] = {}

        # ---- helpers ----
        def _norm_col(c: str) -> str:
            # collapse multiple spaces, trim
            return re.sub(r"\s+", " ", str(c)).strip()

        def _ensure_ids_and_time(df: pd.DataFrame) -> pd.DataFrame:
            out = df.copy()

            # sample_id
            sid_col = next((c for c in out.columns
                            if str(c).lower() in ("sample_id","identifier 1","identifier","sample id","sampleid","sample name","name")), None)
            if sid_col:
                sid = out[sid_col].astype(str)
            else:
                sid = out.index.astype(str)
            sid = (sid.fillna("")
                    .str.strip()
                    .str.replace(r"\s+", " ", regex=True)
                    .str.split("/", n=1, expand=True)[0]
                    .str.strip())
            out["sample_id"] = sid

            # timestamp from Date + Time (or fallback)
            dcol = next((c for c in out.columns if str(c).lower() in ("date","acquisition date","run date")), None)
            tcol = next((c for c in out.columns if str(c).lower() in ("time","acquisition time","run time")), None)
            ts = None
            if dcol and tcol:
                try:
                    ts = pd.to_datetime(out[dcol].astype(str) + " " + out[tcol].astype(str), errors="coerce")
                except Exception:
                    ts = None
            if ts is None and dcol:
                ts = pd.to_datetime(out[dcol], errors="coerce")
            if ts is None:
                ts = pd.to_datetime(np.arange(len(out)), unit="s")
            out["timestamp"] = ts

            # ints: Line / block_no when present
            for icol in ("Line", "block_no"):
                if icol in out.columns:
                    out[icol] = pd.to_numeric(out[icol], errors="coerce").round().astype("Int64")

            return out

        def _alias_iso_from_columns(df: pd.DataFrame) -> set[str]:
            """
            Create canonical d18O/dD/d17O columns when we see vendor-style headers.
            Returns the set of isotopes we added/found.
            """
            added: set[str] = set()
            # build a lookup on normalized lower-case col names
            lower = {c.lower(): c for c in df.columns}
            def _match_exact(norm_target: str) -> str | None:
                # match after removing spaces/underscores
                target = re.sub(r"[\s_]+", "", norm_target.lower())
                for low, orig in lower.items():
                    lown = re.sub(r"[\s_]+", "", low)
                    if lown == target:
                        return orig
                return None

            def _add_iso(patterns, iso):
                for pat in patterns:
                    col = _match_exact(pat)
                    if col:
                        df[iso] = pd.to_numeric(df[col], errors="coerce")
                        added.add(iso)
                        return True
                # looser contains search (handles odd spacing)
                for c in df.columns:
                    nc = re.sub(r"\s+", "", str(c).lower())
                    if iso == "d18O" and ("18o" in nc or "delta18o" in nc):
                        df["d18O"] = pd.to_numeric(df[c], errors="coerce"); added.add("d18O"); return True
                    if iso == "d17O" and ("17o" in nc or "delta17o" in nc):
                        df["d17O"] = pd.to_numeric(df[c], errors="coerce"); added.add("d17O"); return True
                    if iso == "dD" and (("2h" in nc) or ("delta" in nc and "d" in nc and "18" not in nc and "17" not in nc)):
                        df["dD"] = pd.to_numeric(df[c], errors="coerce"); added.add("dD"); return True
                return False

            # Your header examples (cover common Isodat exports)
            _add_iso(["d 18o/16o  mean","d18o/16o mean","d18o mean","delta 18o mean","d18o"], "d18O")
            _add_iso(["d 17o/16o  mean","d17o/16o mean","d17o mean","delta 17o mean","d17o"], "d17O")
            _add_iso(["d 2h/1h  mean","d2h/1h mean","d2h mean","delta d mean","dd","d2h"], "dD")
            return added

        def _iso_from_sheetname(name: str) -> str | None:
            n = str(name).lower()
            if re.search(r"(18|δ18|d18)", n): return "d18O"
            if re.search(r"(17|δ17|d17)", n): return "d17O"
            if re.search(r"(2h|deut|d\b)", n): return "dD"
            return None

        # ---- normalize each sheet -> outiso = df ----
        for sname, df in sheets.items():
            if not isinstance(df, pd.DataFrame) or df.empty:
                continue
            df = df.copy()
            df.columns = [_norm_col(c) for c in df.columns]
            df = _ensure_ids_and_time(df)

            # Try to create canonical isotope columns from vendor labels
            present = _alias_iso_from_columns(df)

            # Decide which isotope key to use
            key = None
            if "d18O" in df.columns: key = "d18O"
            if "d17O" in df.columns: key = "d17O" if key is None else key  # keep first found
            if "dD"   in df.columns: key = "dD"   if key is None else key

            if key is None:
                key = _iso_from_sheetname(sname) or "d18O"  # fallback so we never drop the sheet

            outkey = df

        # ensure never empty
        if not out:
            try:
                # last resort: read csv into d18O with ids/time
                
                df = pd.read_csv(path)
                df.columns = [_norm_col(c) for c in df.columns]
                df = _ensure_ids_and_time(df)
                out["d18O"] = df
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

        return out

    def select_data_file(self, path: str = None, *, show_dialog: bool = True):
        """Load a raw data file. If show_dialog=False, uses the given path directly."""

        self._reset_ui_for_new_dataset()

        # Pick filter based on instrument
        instrument_label = self.instrument_combo.currentText() if hasattr(self, "instrument_combo") else ""
        if instrument_label == "IRMS (Thermo DI)":
            filter_str = "Isodat Dual Inlet (*.csv *.xls *.xlsx);;CSV (*.csv);;Excel (*.xls *.xlsx);;All Files (*)"
        elif instrument_label == "IRMS (EA)":
            filter_str = "EA Exports (*.csv *.xls *.xlsx);;CSV (*.csv);;Excel (*.xls *.xlsx);;All Files (*)"
        else:
            filter_str = "Instrument Data (*.csv *.txt *.xls *.xlsx);;CSV (*.csv);;Text (*.txt);;Excel (*.xls *.xlsx);;All Files (*)"

        if show_dialog:
            file_name, _ = QFileDialog.getOpenFileName(self, "Select Data File", "", filter_str)
            if not file_name:
                return
        else:
            if not path:
                QMessageBox.warning(self, "No Path", "No data file path was provided.")
                return
            file_name = path

        try:
            if hasattr(self, "_record_browse_result"):
                self._record_browse_result(file_name, dir_key="paths/data_dir", file_key="paths/last_data_file")
            else:
                s = getattr(self, "settings", None)
                if s:
                    s.setValue("paths/last_data_file", str(file_name))
                    s.setValue("paths/data_dir", os.path.dirname(file_name))
                    s.sync()
        except Exception as e:
            logging.warning(f"Exception caught: {e}")

        try:
            if hasattr(self, "data_file_edit") and self.data_file_edit:
                self.data_file_edit.setText(file_name)
                self.data_file_edit.setToolTip(file_name)
        except Exception as e:
            logging.warning(f"Exception caught: {e}")

        self.data_file = file_name
        self.data_file_edit.setText(file_name)
        try:
            self.detect_instrument_from_file(file_name)
        except Exception as e:
            logging.warning(f"Exception caught: {e}")
        instrument_label = self.instrument_combo.currentText()

        self.standards_file = None
        self.standards_data = None
        self.processed_data = None
        self.injection_data = None
        self.analysis_data = None
        self.standards_file_edit.clear()
        self.update_standards_table(None)
        self._ensure_post_ui()
        self.reset_postprocess(hide_tab=True)
        self._sync_post_tab_visibility()

        try:
            if instrument_label == "IRMS (EA)":
                self._load_ea_data()
            elif instrument_label == "IRMS (Thermo DI)":
                self._load_di_data()
            else:
                self._load_laser_data()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load data file: {e}")
            logging.error(f"File loading error: {e}", exc_info=True)

    def _load_ea_data(self):
        """Load IRMS (EA) data from Excel (multi-sheet) or CSV."""
        self.memory_fits = {}; self.drift_fits = {}; self.calibration_fits = {}
        if not hasattr(self, "post_cfg") or self.post_cfg is None:
            self.post_cfg = IRMSPostConfig()

        ext = os.path.splitext(self.data_file)[1].lower()
        self.ea_raw_by_iso = {}
        options_map, chosen_map = {}, {}

        if ext in (".xlsx", ".xls"):
            xls = pd.ExcelFile(self.data_file)
            for sname in xls.sheet_names:
                try:
                    sdf = pd.read_excel(xls, sheet_name=sname)
                    if not isinstance(sdf, pd.DataFrame) or sdf.empty:
                        continue
                    sdf = self._ensure_ids_and_time(sdf, mode="EA")
                    try:
                        sdf = resolve_columns(sdf, instrument=ResolverInstrument.IRMS_EA)
                    except Exception as e:
                        logging.warning(f"Exception caught: {e}")
                    isos = self._detect_isos_in_df(sdf, mode="EA")
                    if not isos:
                        continue
                    for iso in isos:
                        sdf_iso = sdf
                        if iso in sdf.columns:
                            m = pd.to_numeric(sdf[iso], errors="coerce").notna()
                            if m.any():
                                sdf_iso = sdf[m].copy()
                        peak_pref = None
                        try: peak_pref = self._cfg_ea_peak(iso)
                        except Exception as e:
                            logging.warning(f"Exception caught: {e}")
                        df_raw, df_filt, iso_out, chosen_peak, peak_opts = prepare_ea_single_isotope(
                            sdf_iso, isotope=iso, peak=peak_pref, cfg=self.post_cfg
                        )
                        iso_key = _iso_norm(iso_out)
                        self.ea_raw_by_iso[iso_key] = self._ensure_ids_and_time(df_raw, mode="EA")
                        options_map[iso_key] = list(peak_opts or [])
                        chosen_map[iso_key] = chosen_peak
                except Exception as e:
                    logging.debug(f"EA sheet '{sname}' skipped: {e}")

            if not self.ea_raw_by_iso:
                QMessageBox.warning(self, "EA Load", "No recognizable EA sheets found.")
                return

            isos = sorted(self.ea_raw_by_iso.keys())
            self._ensure_active_isotope_selector()
            self.active_iso_combo.blockSignals(True)
            self.active_iso_combo.clear()
            self.active_iso_combo.addItems(isos)
            prev = getattr(self, "current_isotope", None)
            self.active_iso_combo.setCurrentText(prev if prev in isos else isos[0])
            self.current_isotope = self.active_iso_combo.currentText()
            self.active_iso_row_widget.setVisible(True)
            self.active_iso_combo.setVisible(True)
            self.active_iso_combo.blockSignals(False)

            try:
                self._ea_rebuild_peak_frame(isos, options_map, chosen_map)
            except Exception as e:
                logging.debug(f"EA peak frame build skipped: {e}")

            cur_iso = self.current_isotope
            raw_cur = self.ea_raw_by_iso[cur_iso]
            peak_pref = None
            try: peak_pref = self._cfg_ea_peak(cur_iso)
            except Exception as e:
                logging.warning(f"Exception caught: {e}")
            df_raw, df_filt, _, _, _ = prepare_ea_single_isotope(raw_cur, isotope=cur_iso, peak=peak_pref, cfg=self.post_cfg)

            self.data = df_raw.copy()
            self.injection_data = df_raw.copy()
            self.analysis_data = df_filt.copy()

            if not hasattr(self, "ea_analysis_by_iso"):
                self.ea_analysis_by_iso = {}
            self.ea_analysis_by_iso[cur_iso] = self.analysis_data.copy(deep=True)

            self.update_data_table(self.injection_table, self.data)
            self.update_data_table(self.analysis_table, self.analysis_data)
            try: self._freeze_ea_raw()
            except Exception as e:
                logging.warning(f"Exception caught: {e}")
            try:
                idx = self.output_tabs.indexOf(self.injection_data_tab)
                if idx != -1: self.output_tabs.setTabText(idx, "IRMS EA Raw Data")
            except Exception as e:
                logging.warning(f"Exception caught: {e}")
            self.update_plot_configs()
            set_status(self.status_label, "Status: EA multi-sheet data loaded.", "success")
            return

        # ---- CSV (single-sheet) fallback ----
        raw_in = pd.read_csv(self.data_file)
        raw_in = self._ensure_ids_and_time(raw_in, mode="EA")
        try:
            raw_in = resolve_columns(raw_in, instrument=ResolverInstrument.IRMS_EA)
        except Exception as e:
            logging.warning(f"Exception caught: {e}")

        isos = self._detect_isos_in_df(raw_in, mode="EA")
        iso = _iso_norm(isos[0] if isos else "d15N")
        peak_pref = None
        try: peak_pref = self._cfg_ea_peak(iso)
        except Exception as e:
            logging.warning(f"Exception caught: {e}")
        df_raw, df_filt, iso_out, chosen_peak, peak_opts = prepare_ea_single_isotope(
            raw_in, isotope=iso, peak=peak_pref, cfg=self.post_cfg
        )
        iso = _iso_norm(iso_out)

        self.ea_raw_by_iso = {iso: self._ensure_ids_and_time(df_raw, mode="EA")}
        self._ensure_active_isotope_selector()
        self.active_iso_combo.blockSignals(True)
        self.active_iso_combo.clear()
        self.active_iso_combo.addItem(iso)
        self.active_iso_combo.setCurrentText(iso)
        self.current_isotope = iso
        self.active_iso_row_widget.setVisible(True)
        self.active_iso_combo.setVisible(True)
        self.active_iso_combo.blockSignals(False)

        try:
            self._ea_rebuild_peak_frame(iso, {iso: list(peak_opts or [])}, {iso: chosen_peak})
        except Exception as e:
            logging.warning(f"Exception caught: {e}")

        self.data = df_raw.copy()
        self.injection_data = df_raw.copy()
        self.analysis_data = df_filt.copy()
        self.update_data_table(self.injection_table, self.data)
        self.update_data_table(self.analysis_table, self.analysis_data)
        try: self._freeze_ea_raw()
        except Exception as e:
            logging.warning(f"Exception caught: {e}")
        self.update_plot_configs()
        set_status(self.status_label, "Status: EA data loaded.", "success")

    def _load_di_data(self):
        """Load IRMS (Thermo DI) data."""
        self.memory_fits = {}; self.drift_fits = {}; self.calibration_fits = {}
        try:
            self.di_raw_by_iso = self._load_di_multisheet(self.data_file)
        except Exception as e:
            logging.error(f"DI load failed: {e}", exc_info=True)
            QMessageBox.critical(self, "DI Load", f"Failed to read DI file:\n{e}")
            return

        if not isinstance(self.di_raw_by_iso, dict) or not self.di_raw_by_iso:
            QMessageBox.warning(self, "DI Load", "No recognizable DI sheets found.")
            return

        isos = sorted(self.di_raw_by_iso.keys())
        first_iso = isos[0]
        self._di_isotopes = list(isos)
        self._is_di_multisheet = True

        prev = getattr(self, "current_isotope", None)
        active_iso = prev if prev in isos else first_iso
        self.current_isotope = active_iso

        self.data = self.di_raw_by_iso[active_iso].copy()
        self.analysis_data = pd.DataFrame()
        self.di_analysis_by_iso = {}

        self._populate_isotope_selector_from(None, candidates=self._di_isotopes)
        try:
            self.active_iso_combo.blockSignals(True)
            self.active_iso_combo.clear()
            self.active_iso_combo.addItems(isos)
            self.active_iso_combo.setCurrentText(active_iso)
            self.active_iso_row_widget.setVisible(True)
            self.active_iso_combo.setVisible(True)
        finally:
            self.active_iso_combo.blockSignals(False)

        if not hasattr(self, "_di_iso_signal_connected"):
            self.active_iso_combo.currentTextChanged.connect(self._on_di_active_iso_changed)
            self._di_iso_signal_connected = True

        self.update_data_table(self.injection_table, self.data)
        self.update_data_table(self.analysis_table, pd.DataFrame())
        try:
            idx = self.output_tabs.indexOf(self.injection_data_tab)
            if idx != -1:
                self.output_tabs.setTabText(idx, "IRMS DI Raw Data")
        except Exception as e:
            logging.warning(f"Exception caught: {e}")

        try:
            if hasattr(self, "figure") and self.figure is not None: self.figure.clf()
            if hasattr(self, "canvas") and self.canvas is not None: self.canvas.draw_idle()
        except Exception as e:
            logging.warning(f"Exception caught: {e}")
        self.update_plot_configs()

        try:
            self._sync_post_tab_visibility()
        except Exception as e:
            logging.warning(f"Exception caught: {e}")
        set_status(self.status_label, "Status: IRMS DI (multi-sheet) data loaded.", "success")
        self.output_tabs.setCurrentWidget(self.injection_data_tab)

    def _load_laser_data(self):
        """Load Laser (LGR / Picarro) data."""
        self.memory_fits = {}
        self.drift_fits = {}

        raw_df = pd.read_csv(self.data_file)
        instrument = InstrumentType(self.instrument_combo.currentText())

        # Heuristic: EA CSV accidentally chosen in Laser mode
        ea_cols = set(map(str, raw_df.columns))
        if {"Gasconfiguration", "Identifier 1"}.issubset(ea_cols):
            result = irms_load(self.data_file)
            self.data = resolve_columns(result.vendor_table.copy(), instrument=ResolverInstrument.IRMS_EA)
            self.data = self._ensure_ids_and_time(self.data, mode="EA")
            self.update_data_table(self.injection_table, self.data)
            try: self.output_tabs.setTabText(0, "EA Raw Data")
            except Exception as e:
                logging.warning(f"Exception caught: {e}")
            if hasattr(self, "isotope_group"):
                self.isotope_group.setVisible(False)
            self.update_plot_configs()
            set_status(self.status_label, "Status: EA data loaded (auto-detected). Standards not required.")
            self.output_tabs.setCurrentWidget(self.injection_data_tab)
            return

        # Normal Laser path
        self.data = map_and_clean_raw_data(raw_df, instrument)
        self.data = resolve_columns(self.data, instrument=instrument)
        self.data = self._ensure_ids_and_time(self.data, mode="DI")

        self.update_data_table(self.injection_table, self.data)
        try: self.output_tabs.setTabText(0, "Raw Data")
        except Exception as e:
            logging.warning(f"Exception caught: {e}")

        try:
            if hasattr(self, "figure") and self.figure is not None:
                self.figure.clf()
            if hasattr(self, "canvas") and self.canvas is not None:
                self.canvas.draw_idle()
        except Exception as e:
            logging.warning(f"Exception caught: {e}")

        self.update_plot_configs()
        if hasattr(self, "d18O_check"):
            self.d18O_check.setVisible('d18O' in self.data.columns)
            self.dD_check.setVisible('dD' in self.data.columns)
            self.d17O_check.setVisible('d17O' in self.data.columns)
            self.isotope_group.setVisible(True)
        try:
            self.output_tabs.setCurrentWidget(self.injection_data_tab)
        except Exception as e:
            logging.warning(f"Exception caught: {e}")

        try:
            if getattr(self, "data", None) is not None:
                self._populate_isotope_selector_from(self.data)
                self.isotope_combo = self.active_iso_combo
                self._show_active_isotope_selector(True)
            self.update_plot_configs()
            if hasattr(self, "_refresh_tables_for_active_isotope"):
                self._refresh_tables_for_active_isotope()
        except Exception as e:
            logging.warning(f"Exception caught: {e}")

        try:
            self._sync_post_tab_visibility()
        except Exception as e:
            logging.warning(f"Exception caught: {e}")

    @staticmethod
    def _ensure_ids_and_time(df: pd.DataFrame, *, mode: str) -> pd.DataFrame:
        """Create/clean sample_id, timestamp, and int-like cols."""
        out = df.copy()

        # ----- sample_id -----
        candidates = ["sample_id", "Identifier 1", "identifier_1", "Sample ID", "SampleID", "Sample Name", "name", "ref_name"]
        sid = None
        for c in candidates:
            if c in out.columns:
                sid = out[c].astype(str)
                break
        if sid is None:
            sid = out.index.astype(str)
        sid = sid.fillna("").str.strip()
        sid = sid.str.split("/", n=1, expand=True)[0].str.strip()
        out["sample_id"] = sid

        # ----- timestamp -----
        if "timestamp" in out.columns:
            try:
                out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
            except Exception as e:
                logging.warning(f"Exception caught: {e}")
        else:
            date_cols = [c for c in out.columns if str(c).lower() in ("date", "acquisition date", "run date")]
            time_cols = [c for c in out.columns if str(c).lower() in ("time", "acquisition time", "run time")]
            ts = None
            if date_cols and time_cols:
                try:
                    dser = pd.to_datetime(out[date_cols[0]], errors="coerce")
                    tser = pd.to_datetime(out[time_cols[0]], errors="coerce").dt.time
                    ts = pd.to_datetime(dser.astype(str) + " " + pd.Series(tser).astype(str), errors="coerce")
                except Exception:
                    ts = None
            if ts is None and date_cols:
                try:
                    ts = pd.to_datetime(out[date_cols[0]], errors="coerce")
                except Exception:
                    ts = None
            if ts is None:
                ts = pd.to_datetime(pd.Series(range(len(out))), unit="s")
            out["timestamp"] = ts

        # ----- EA aliases & ints -----
        if mode == "EA":
            if "Peak Nr" in out.columns and "Peak" not in out.columns:
                out = out.rename(columns={"Peak Nr": "Peak"})
            for c in ("Line", "Peak", "Peak Nr", "block_no", "Analysis", "injection_no", "tray_vial_position"):
                if c in out.columns:
                    out[c] = pd.to_numeric(out[c], errors="coerce").astype("Int64")

        # ----- DI minor ints -----
        if mode == "DI":
            for c in ("block_no", "injection_no"):
                if c in out.columns:
                    out[c] = pd.to_numeric(out[c], errors="coerce").astype("Int64")

        return out

    @staticmethod
    def _detect_isos_in_df(df: pd.DataFrame, *, mode: str) -> list[str]:
        _DI_ISOS = ("d18O", "dD", "d17O")
        _EA_ISOS = ("d13C", "d15N", "d34S")
        cols = {str(c).lower() for c in df.columns}
        if mode == "DI":
            return [iso for iso in _DI_ISOS if (iso.lower() in cols) or any(c.startswith(iso.lower() + "_") for c in cols)]
        found = [iso for iso in _EA_ISOS if (iso.lower() in cols) or any(c.startswith(iso.lower() + "_") for c in cols)]
        if not found:
            if any("15n" in c for c in cols): found.append("d15N")
            if any("13c" in c for c in cols): found.append("d13C")
            if any("34s" in c for c in cols): found.append("d34S")
        return found

    def _order_analysis_columns(self, df):
        """Move timestamp, cumulative_injection, water_conc to the end (if present)."""
        
        if not isinstance(df, pd.DataFrame) or df.empty:
            return df
        cols = list(map(str, df.columns))
        tail = [c for c in ["sample_name" ,"timestamp", "cumulative_injection", "water_conc"] if c in cols]
        head = [c for c in cols if c not in tail]
        return df[head + tail]

    def _order_standard_columns(self, df):
        """Reorder standards table columns so IDs/roles appear first, then certified values, then other fields."""
        
        if not isinstance(df, pd.DataFrame) or df.empty:
            return df

        cols = list(map(str, df.columns))

        # Preferred ID/display columns (keep only those that exist, in this order)
        id_pref = [c for c in ["sample_id", "ref_name", "name", "id"] if c in cols]

        # Role columns (boolean flags or role codes)
        role_cols = [c for c in cols if c.lower().startswith("is_")] + \
                    [c for c in cols if c.lower() in ("role_code", "role", "rolecode")]

        # Certified value & uncertainty columns (wide or flexible schema)
        value_cols = [c for c in cols if c.endswith("_true") or c.endswith("_uncertainty")]
        # If user stores values as plain d18O, d13C, etc., show those next (but avoid duplicates)
        isotopes = ("d18O","dD","d17O","d13C","d15N","d2H")
        plain_iso_cols = [c for c in cols if c in isotopes and c not in value_cols]

        # Everything else that we haven't placed yet
        placed = set(id_pref + role_cols + value_cols + plain_iso_cols)
        other_cols = [c for c in cols if c not in placed]

        # De-duplicate while preserving order
        def _dedup(seq): 
            seen=set(); out=[]
            for x in seq:
                if x not in seen:
                    out.append(x); seen.add(x)
            return out

        ordered = _dedup(id_pref + role_cols + value_cols + plain_iso_cols + other_cols)
        return df[ordered]

    def select_standards_file(self, path: str = None, *, show_dialog: bool = True):
        """Load standards (CSV/JSON/XLSX). Filter to current dataset sample_ids; do NOT process."""
        # Pick file (dialog or provided path)
        if show_dialog:
            picked, _ = QFileDialog.getOpenFileName(
                self, "Select Standards File", "",
                "Standards (*.csv *.json *.xlsx *.xls);;All Files (*)"
            )
            if not picked:
                return
            path = picked
        else:
            if not path or not os.path.exists(path):
                QMessageBox.warning(self, "Missing Standards File", "Please choose a valid Standards file.")
                return

        try:
            # Load (flexible reader may return (df, role_map) or just df)
            res = read_standards_flexible(path)
            if isinstance(res, tuple) and len(res) == 2:
                std_long, role_map = res
            else:
                std_long, role_map = res, {}

            # Filter to current dataset sample_ids (prefer Analysis → Raw)
            ids = []
            for name in ("analysis_data", "injection_data", "data"):
                df = getattr(self, name, None)
                if isinstance(df, pd.DataFrame) and "sample_id" in df.columns and not df.empty:
                    ids.extend(df["sample_id"].astype(str).dropna().tolist())
            ids_set = {s.strip() for s in ids if str(s).strip()}
            std_view = std_long
            if ids_set and isinstance(std_long, pd.DataFrame) and "sample_id" in std_long.columns:
                std_view = std_long.loc[std_long["sample_id"].astype(str).isin(ids_set)].copy()

            # --- Ensure iso-specific true columns (d18O_true, dD_true, ...) for calibration parity ---
            try:
                s = std_view.copy()
                if "sample_id" in s.columns:
                    s["sample_id"] = s["sample_id"].astype(str).str.strip()

                canonical = ("d18O","dD","d17O","d13C","d15N")
                have_true_cols = any(f"{iso}_true" in s.columns for iso in canonical)

                if not have_true_cols:
                    # Case A: long standards -> pivot isotope/true to wide *_true
                    if "isotope" in s.columns and "true" in s.columns:
                        t = (
                            s.loc[~s["true"].isna(), ["sample_id", "isotope", "true"]]
                            .assign(isotope=lambda d: d["isotope"].astype(str))
                        )
                        if not t.empty:
                            wide = (
                                t.pivot_table(index="sample_id", columns="isotope", values="true", aggfunc="first")
                                .rename(columns={iso: f"{iso}_true" for iso in t["isotope"].unique()})
                                .reset_index()
                            )
                            s = s.merge(wide, on="sample_id", how="left")

                    # Case B: wide standards -> copy d18O -> d18O_true etc.
                    for iso in canonical:
                        raw_col = iso
                        true_col = f"{iso}_true"
                        if raw_col in s.columns and true_col not in s.columns:
                            s[true_col] = pd.to_numeric(s[raw_col], errors="coerce")

                    std_view = s
            except Exception as _e:
                logging.warning(f"Standards true-column normalization skipped: {_e}")
            # --- END true-target normalization ---

            # Save for now (will replace with run-ordered version below)
            self.standards_file = path
            self.role_map = role_map
            self.standards_data = std_view

            # --- Align standards row order to the run order from the data ---
            try:
                run = getattr(self, "injection_data", None)
                if not isinstance(run, pd.DataFrame) or run.empty:
                    run = getattr(self, "data", None)

                if isinstance(run, pd.DataFrame) and not run.empty and "sample_id" in run.columns:
                    s = self.standards_data.copy()
                    # String IDs to ensure matching
                    s["sample_id"] = s["sample_id"].astype(str)
                    run_ids = run.copy()
                    run_ids["sample_id"] = run_ids["sample_id"].astype(str)

                    # Choose an order column from data, else synthetic index
                    ord_col = "order" if "order" in run_ids.columns else ("Analysis" if "Analysis" in run_ids.columns else None)
                    if ord_col is None:
                        run_ids = run_ids.reset_index().rename(columns={"index": "__ord__"})
                        ord_col = "__ord__"
                    else:
                        run_ids[ord_col] = pd.to_numeric(run_ids[ord_col], errors="coerce")
                        if run_ids[ord_col].isna().all():
                            run_ids = run_ids.reset_index().rename(columns={"index": "__ord__"})
                            ord_col = "__ord__"

                    # First occurrence per sample_id in run order
                    ord_map = (
                        run_ids[["sample_id", ord_col]]
                        .sort_values(ord_col, kind="mergesort")
                        .drop_duplicates("sample_id")
                    )

                    # Merge + sort by run order (group by isotope if present)
                    s = s.merge(ord_map, on="sample_id", how="left")
                    s[ord_col] = pd.to_numeric(s[ord_col], errors="coerce")
                    fill_val = (s[ord_col].max(skipna=True) if not s[ord_col].dropna().empty else 0) + 1
                    s[ord_col] = s[ord_col].fillna(fill_val)
                    sort_keys = ["isotope", ord_col] if "isotope" in s.columns else ord_col
                    s = s.sort_values(sort_keys, kind="mergesort")
                    if ord_col == "__ord__":
                        s = s.drop(columns=ord_col)

                    self.standards_data = s
            except Exception as e:
                logging.warning(f"Standards run-order sort skipped: {e}")
            # --- END: run-order sort ---

            # Rebuild GUI table from the (possibly re-ordered) standards_data
            gui_df = self._standards_to_gui_table(self.standards_data, self.role_map)

            # UI reflect + persist
            if hasattr(self, "standards_file_edit") and self.standards_file_edit:
                self.standards_file_edit.setText(path)
                self.standards_file_edit.setToolTip(path)
            try:
                s = getattr(self, "settings", None)
                if s:
                    s.setValue("paths/standards_file", str(path))
                    s.sync()
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

            # Update table and keep current data views intact
            self.update_standards_table(gui_df, from_lims=False)
            self._show_raw_table()
            self._refresh_input_panel()
            self._update_load_both_enabled()

            set_status(self.status_label,f"Status: Standards loaded ({len(self.standards_data)} rows shown).", "success")

        except Exception as e:
            logging.error(f"Failed to load standards file: {e}", exc_info=True)
            QMessageBox.critical(self, "Standards Error", f"Could not parse standards file:\n{e}")

    def _get_dsn_text(self) -> str:
        w = getattr(self, "dsn_file_edit", None) or getattr(self, "dsn_edit", None)
        return w.text().strip() if w else ""

    def _get_run_id_text(self) -> str:
        w = getattr(self, "run_id_edit", None) or getattr(self, "run_no_edit", None)
        return w.text().strip() if w else ""

    def load_batch_from_lims(self):
        """Fetch batch info from LIMS, let user pick the raw file (seeded with last data dir),
        enrich & display, and persist DSN/data folders for future browsing."""

        dsn_path = self._get_dsn_text()
        dsn_path = dsn_path.strip(';')
        # If DSN is '0' or empty (from CLI arg), try to recover from Settings
        if not dsn_path or dsn_path == "0":
            dsn_path = self.settings.value("lims/dsn_path", type=str)
            if dsn_path and hasattr(self, "dsn_edit"):
                self.dsn_edit.setText(dsn_path)       
                 
        dsn_path = dsn_path.strip(';') if dsn_path else ""
        batch_id = self._get_run_id_text()
        
        if not dsn_path or not batch_id:
            QMessageBox.warning(self, "Missing Input", "Please provide DSN and Run No.")
            return

        # --- Persist DSN + its folder (so DSN browse starts here next time)
        try:
            s = getattr(self, "settings", None)
            if s:
                s.setValue("lims/dsn_path", dsn_path)
                s.setValue("lims/run_id", batch_id)
                s.setValue("paths/lims_dir", os.path.dirname(dsn_path))
                s.sync()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # --- Start folder for raw-file dialog (last data dir or last data file’s dir)
        try:
            if hasattr(self, "_dialog_start_dir"):
                start_dir = self._dialog_start_dir("paths/data_dir")
            else:
                s = getattr(self, "settings", None)
                start_dir = ""
                if s:
                    d = s.value("paths/data_dir", type=str)
                    if d and os.path.isdir(d):
                        start_dir = d
                    else:
                        lastf = s.value("paths/last_data_file", type=str)
                        if lastf and os.path.isfile(lastf):
                            start_dir = os.path.dirname(lastf)
                if not start_dir:
                    start_dir = os.path.expanduser("~")
        except Exception:
            start_dir = ""

        # --- Pick the raw data file (seeded with start_dir)
        data_file, _ = QFileDialog.getOpenFileName(
            self,
            "Select the Raw Data File for this Batch",
            start_dir,
            "CSV Files (*.csv);;All Files (*)"
        )
        if not data_file:
            return
        self.data_file = data_file

        # Persist chosen file + its folder for future browsing
        try:
            if hasattr(self, "_record_browse_result"):
                self._record_browse_result(data_file, dir_key="paths/data_dir", file_key="paths/last_data_file")
            else:
                s = getattr(self, "settings", None)
                if s:
                    s.setValue("paths/last_data_file", data_file)
                    s.setValue("paths/data_dir", os.path.dirname(data_file))
                    s.sync()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        # Reflect in the LIMS “Selected data file” textbox if present
        try:
            if hasattr(self, "_lims_set_data_file_path"):
                self._lims_set_data_file_path(self.data_file or f"LIMS batch {batch_id}")
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        # --- LIMS fetch + mapping + enrichment
        try:
            set_status(self.status_label,f"Status: Connecting to LIMS (Batch {batch_id})...", "processing")
            QApplication.processEvents()

            # 1. INITIALIZE THE DATABASE ENGINE
            if dsn_path.startswith(('postgresql://', 'postgresql+')):
                dialect = "POSTGRESQL"
            elif dsn_path.lower().endswith(('.mdb', '.accdb')):
                dialect = "ACCESS"
            else:
                dialect = "SQL_SERVER"

            # Initialize the shared pool (skip if already initialized with same dialect)
            if not db_manager._engine or db_manager.dialect != dialect:
                db_manager.initialize(dialect, dsn_path)

            # 2. CALL DATABASE WITHOUT DSN PATH
            loadlist_df, standards_df = database.get_batch_data(dsn_path, batch_id)
            # loadlist_df, standards_df = database.get_batch_data(int(batch_id))
            
            self.standards_data = standards_df.copy()
            self.lims_loadlist = loadlist_df.copy()
            
            # (The rest of this method remains UNCHANGED)
            for key in ("SIAnalysisRunID","AnalysisRunID","RunID"):
                if key in self.lims_loadlist.columns:
                    try:
                        self.si_analysis_run_id = int(pd.to_numeric(self.lims_loadlist[key], errors="coerce").dropna().iloc[0])
                        break
                    except Exception as e:

                        logging.warning(f"Exception caught: {e}")
            self._update_save_to_db_enabled()
            
            # Respect user/standards flags if present; only auto-assign if none exist
            try:
                std = self.standards_data.copy()
                for col in ("is_calibration_id","is_memory_id","is_drift_id","is_linearity_id"):
                    if col in std.columns:
                        std[col] = pd.to_numeric(std[col], errors="coerce").fillna(0).astype(int)
                    else:
                        std[col] = 0
                has_any_flags = (std[["is_calibration_id","is_memory_id","is_drift_id","is_linearity_id"]].sum().sum() > 0)
                self.standards_data = std
                # If has_any_flags is False, keep your existing auto-flagging logic;
                # if it's True, skip auto-flagging to preserve user selections.
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            
            raw_df = pd.read_csv(self.data_file)
            instrument = InstrumentType(self.instrument_combo.currentText())
            mapped_data = map_and_clean_raw_data(raw_df, instrument)

            # Raw/analysis base for lasers
            self.data = prepare_raw_data_for_plotting(mapped_data, self.standards_data, instrument)
            self.data = database.enrich_data_with_lims_info(self.data, loadlist_df)
            self.data = resolve_columns(self.data, instrument=self.instrument_combo.currentText(),)

            # Make sure tables use an explicit raw pointer
            self.injection_data = self.data.copy()
            
            # Tables
            try:
                iso = getattr(self, "current_isotope", None)
                view = self._view_for_isotope(self.injection_data, iso) if iso else self.injection_data
            except Exception:
                view = self.injection_data
            self.update_data_table(self.injection_table, view)
            self.update_standards_table(self.standards_data, from_lims=True)

            # Optional numeric formatting on standards table
            try:
                for c in range(self.standards_table.columnCount()):
                    hdr = self.standards_table.horizontalHeaderItem(c).text()
                    if any(k in hdr for k in ["d18O", "dD", "d17O", "d13C", "d15N"]) and not hdr.startswith("is_"):
                        for r in range(self.standards_table.rowCount()):
                            it = self.standards_table.item(r, c)
                            if it is not None:
                                try:
                                    val = float(it.text()); it.setText(f"{val:.3f}")
                                except Exception as e:

                                    logging.warning(f"Exception caught: {e}")
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

            # UI state
            self.output_tabs.setTabText(0, "Raw Data")
            self.output_tabs.setTabEnabled(3, True)
            self.update_plot_configs()
            self.isotope_group.setVisible(True)
            self._show_raw_table()
            self._populate_isotope_selector_from(self.data)
            self._refresh_input_panel()

            set_status(self.status_label,f"Status: Batch {batch_id} loaded successfully. Ready to process.", "success")
        except Exception as e:
            QMessageBox.critical(self, "LIMS Error", f"Failed to load batch from LIMS: {e}")
            logging.error(f"LIMS loading error: {e}", exc_info=True)
            set_status(self.status_label,"Status: Error loading from LIMS.", "error")

    def get_roles_from_table(self):
        if self.standards_table.rowCount() == 0: return {}
        roles = {'linearity': None, 'drift': None, 'memory': [], 'calibration': [], 'validation': []}
        headers = [self.standards_table.horizontalHeaderItem(c).text().lower() for c in range(self.standards_table.columnCount())]
        try:
            id_col = headers.index('sample_id'); lin_col = headers.index('is_linearity_id'); drift_col = headers.index('is_drift_id'); mem_col = headers.index('is_memory_id'); cal_col = headers.index('is_calibration_id'); con_col = headers.index('is_control_id')
        except ValueError as e:
            QMessageBox.critical(self, "Column Error", f"A required 'is_...' or 'sample_id' column name is misspelled or missing from the standards file: {e}"); return None
        for row in range(self.standards_table.rowCount()):
            sample_id = self.standards_table.item(row, id_col).text()
            if self.standards_table.item(row, lin_col).checkState() == Qt.Checked: roles['linearity'] = sample_id
            if self.standards_table.item(row, drift_col).checkState() == Qt.Checked: roles['drift'] = sample_id
            if self.standards_table.item(row, mem_col).checkState() == Qt.Checked: roles['memory'].append(sample_id)
            if self.standards_table.item(row, cal_col).checkState() == Qt.Checked: roles['calibration'].append(sample_id)
            if self.standards_table.item(row, con_col).checkState() == Qt.Checked: roles['validation'].append(sample_id)
        # logging.info(f"Roles read from GUI: {roles}")
        return roles
       
    def _set_instrument_combo_silent(self, label: str):
        """Set the instrument dropdown to `label` without emitting signals."""
        try:
            cur = self.instrument_combo.currentText()
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return
        if not label or cur == label:
            return
        self.instrument_combo.blockSignals(True)
        try:
            
            idx = self.instrument_combo.findText(label, Qt.MatchFixedString)
            if idx >= 0:
                self.instrument_combo.setCurrentIndex(idx)
        finally:
            self.instrument_combo.blockSignals(False)
   
    def _set_validation_status(self, summary: dict | None):
        """Writes validation summary to the Plots/Reports tab label if present."""
        if not summary:
            text = "Validation results: Not processed"
        else:
            parts = []
            for iso, s in summary.items():
                n = s.get("n", 0)
                maxz = s.get("max_abs_z", float("nan"))
                fails = s.get("n_fail", 0)
                parts.append(f"{iso}: n={n}, max|z|={maxz:.2f}, fails={fails}")
            text = "Validation: " + " | ".join(parts)

        if hasattr(self, "plot_status_label") and self.plot_status_label:
            self.plot_status_label.setText(text)
        else:
            # fallback if Plots label not available
            set_status(self.status_label,text, "error")  

    def _get_combo_text(self, attr_name: str, default: str = "Off") -> str:
        w = getattr(self, attr_name, None)
        try:
            return w.currentText() if w is not None else default
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return default
        
    def _get_check(self, name: str, default: bool = False) -> bool:
        w = getattr(self, name, None)
        try:
            return bool(w.isChecked()) if w is not None else default
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return default

    # --- Post-processing refresh helpers ---------------------------------
    def _active_instrument_label(self) -> str:
        try:
            return self.instrument_combo.currentText()
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return ""

    def start_processing(self):
        """Run processing for IRMS DI, IRMS EA, or Laser (LGR/Picarro) with unified calibration and multi-sheet support."""
        # ---- clear canvas (safe) ----
        try:
            if hasattr(self, "figure") and self.figure is not None:
                self.figure.clf()
            if hasattr(self, "canvas") and self.canvas is not None:
                self.canvas.draw_idle()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # Preserve UI if you have these helpers (safe no-op otherwise)
        saved_ui = None
        try:
            if hasattr(self, "_begin_preserve_multi_iso_ui"):
                saved_ui = self._begin_preserve_multi_iso_ui()
        except Exception:
            saved_ui = None
            
        # --- Before heavy work: reset Analysis-selection UI & collapse sidebar
        try:
            if hasattr(self, "analysis_combo") and self.analysis_combo:
                with QSignalBlocker(self.analysis_combo):
                    self.analysis_combo.clear()
                # Hide until plots are rebuilt; update_plot_configs will repopulate if needed
                try:
                    self.analysis_combo.hide()
                    if hasattr(self, "analysis_combo_label") and self.analysis_combo_label:
                        self.analysis_combo_label.hide()
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # Collapse the sidebar to maximize workspace during processing
        try:
            self._set_input_panel_collapsed(True)
        except Exception as e:
            logging.warning(f"Failed to collapse sidebar: {e}")

        # --- PARITY PREFLIGHT (no algorithm changes; normalization + logging only) ---
        try:
            # 1) Normalize Raw/Analysis IDs & roles (if present)
            for attr in ("injection_data", "analysis_data"):
                _df = getattr(self, attr, None)
                if isinstance(_df, pd.DataFrame) and not _df.empty:
                    df = _df.copy()
                    if "sample_id" in df.columns:
                        df["sample_id"] = df["sample_id"].astype(str).str.strip()
                    if "block_no" not in df.columns:
                        df["block_no"] = 1
                    if "role_code" in df.columns:
                        df["role_code"] = df["role_code"].astype(str).str.upper()
                    setattr(self, attr, df)

            # 2) Standards: coerce types, ensure expected flag cols exist, filter to current sample_ids
            std = getattr(self, "standards_data", None)
            if isinstance(std, pd.DataFrame) and not std.empty:
                # std = std.copy()
                if "sample_id" in std.columns:
                    std["sample_id"] = std["sample_id"].astype(str).str.strip()
                if "isotope" in std.columns:
                    std["isotope"] = std["isotope"].astype(str)
                for col in ("is_calibration_id","is_memory_id","is_drift_id","is_linearity_id"):
                    if col not in std.columns:
                        std[col] = 0
                    std[col] = pd.to_numeric(std[col], errors="coerce").fillna(0).astype(int)

                # Filter standards to only sample_ids present in the current dataset
                ids = set()
                inj_df = getattr(self, "injection_data", None)
                ana_df = getattr(self, "analysis_data", None)
                for df in (ana_df, inj_df):
                    if isinstance(df, pd.DataFrame) and "sample_id" in df.columns and not df.empty:
                        ids.update(df["sample_id"].dropna().astype(str))
                if ids and "sample_id" in std.columns:
                    std = std[std["sample_id"].astype(str).isin(ids)].copy()
                self.standards_data = std

            # 3) Determine present isotopes safely
            inj_df = getattr(self, "injection_data", None)
            inj_cols = list(inj_df.columns) if isinstance(inj_df, pd.DataFrame) else []
            present_isos = [iso for iso in ("d18O","dD","d17O","d13C","d15N")
                            if any(str(c).startswith(iso) for c in inj_cols)]

            # 4) Read UI options for logging only
            label = (self.instrument_combo.currentText() or "").lower()
            mwl = self.mwl_combo.currentText() if hasattr(self, "mwl_combo") else "None"
            memory_model = self.memory_model_combo.currentText() if hasattr(self, "memory_model_combo") else "Two-Pool"
            drift_axis = self.drift_axis_combo.currentText() if hasattr(self, "drift_axis_combo") else "order"
            ea_lin   = self.ea_linearity_combo.currentText() if hasattr(self, "ea_linearity_combo") else "Off"
            ea_drift = self.ea_drift_combo.currentText() if hasattr(self, "ea_drift_combo") else "Off"
            ea_robust= bool(self.ea_robust_check.isChecked()) if hasattr(self, "ea_robust_check") else False

            logging.info(f"PROC CFG: instrument='{label}', mwl='{mwl}', memory='{memory_model}', drift='{drift_axis}', ea_lin='{ea_lin}', ea_drift='{ea_drift}', robust={ea_robust}")

            def _ids(std_df, iso, flag):
                try:
                    s = std_df
                    if "isotope" in s.columns:
                        s = s[s["isotope"].astype(str) == str(iso)]
                    if flag in s.columns:
                        return sorted(set(s.loc[s[flag] == 1, "sample_id"].astype(str)))
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")
                return []

            if isinstance(self.standards_data, pd.DataFrame) and not self.standards_data.empty:
                for iso in present_isos:
                    logging.info(
                        f"PARITY INPUT {iso}: "
                        f"cal={_ids(self.standards_data, iso, 'is_calibration_id')} "
                        f"mem={_ids(self.standards_data, iso, 'is_memory_id')} "
                        f"drift={_ids(self.standards_data, iso, 'is_drift_id')}"
                    )
        except Exception as _e:
            logging.warning(f"Parity preflight skipped: {type(_e).__name__}: {_e}")
        # --- END PARITY PREFLIGHT ---

        # --- Standards true-column normalization (ensures d18O_true, dD_true, ...) ---
        std_proc = getattr(self, "standards_data", None)
        if isinstance(std_proc, pd.DataFrame) and not std_proc.empty:            
            stdp = std_proc.copy()
            if "sample_id" in stdp.columns:
                stdp["sample_id"] = stdp["sample_id"].astype(str).str.strip()

            # If standards are in long form with generic 'true', create iso-specific true columns
            if "isotope" in stdp.columns and "true" in stdp.columns:
                for iso in ("d18O","dD","d17O","d13C","d15N"):
                    col = f"{iso}_true"
                    if col not in stdp.columns:
                        sub = stdp.loc[stdp["isotope"].astype(str) == iso, ["sample_id","true"]].dropna()
                        if not sub.empty:
                            sub = sub.drop_duplicates("sample_id")
                            stdp = stdp.merge(sub.rename(columns={"true": col}), on="sample_id", how="left")
            # Use the normalized copy for processing only
            _std_for_processing = stdp
        else:
            _std_for_processing = std_proc
        # --- END true-column normalization ---
           
        try:
            instrument_label = self.instrument_combo.currentText()
            iso = getattr(self, "current_isotope", None)

            # Measured column preference (fallback if not imported)
            try:
                from isotope_processor import DEFAULT_MEASURED_ORDER as measured_order_default
            except Exception:
                measured_order_default = (
                    "_lin_drift_corrected",
                    "_lin_corrected",
                    "_memory_corrected",
                    "_drift_corrected",
                    "_corrected",
                    "",  # raw last
                )

            if instrument_label == "IRMS (Thermo DI)":
                self._run_di_pipeline(_std_for_processing)
            elif instrument_label == "IRMS (EA)":
                self._run_ea_pipeline(_std_for_processing, measured_order_default)
            elif instrument_label in ("LGR", "Picarro"):
                self._run_laser_pipeline(_std_for_processing, measured_order_default)
            else:
                try:
                    if hasattr(self, "_start_processing_other"):
                        self._start_processing_other()
                    if hasattr(self, "start_processing_legacy"):
                        self.start_processing_legacy()
                except Exception as e:
                    logging.warning(f"Exception caught: {e}")
                try:
                    self._reassert_collapsed_chevron()
                except Exception as e:
                    logging.warning(f"Exception caught: {e}")
                QMessageBox.warning(self, "Instrument", "Please choose a valid instrument.")
        except Exception as _proc_err:
            import traceback as _tb
            logging.exception("Processing error")
            QMessageBox.critical(
                self, "Processing Error",
                f"{type(_proc_err).__name__}: {_proc_err}\n\n{_tb.format_exc()}"
            )
        finally:
            self._update_save_to_db_enabled()
            try:
                if hasattr(self, "_end_preserve_multi_iso_ui"):
                    self._end_preserve_multi_iso_ui(saved_ui)
            except Exception as e:
                logging.warning(f"Exception caught: {e}")

    def _run_di_pipeline(self, _std_for_processing):
        """IRMS (Thermo DI) calibration pipeline."""
        d = getattr(self, "data", None)
        if d is None or getattr(d, "empty", False):
            QMessageBox.warning(self, "No Data", "Please load an IRMS DI CSV/XLS first.")
            return

        instrument_label = self.instrument_combo.currentText()
        std_df = getattr(self, "standards_data", None)

        # Optional aliasing for water columns
        def _alias_water(df):
            df = resolve_columns(df, instrument=instrument_label)
            return df

        try:

            calibrated, fits = apply_calibration_generic(
                _alias_water(d.copy()),
                std_df,
                id_col="sample_id",
                isotopes=("d18O", "dD", "d17O"),
                measured_order=("_lin_drift_corrected", "_lin_corrected",
                                "_memory_corrected", "_drift_corrected", "_corrected", ""),
                extra_stage_unc={},
            )
            self.analysis_data = _alias_water(calibrated)
            self.calibration_fits = fits

            try: self.update_data_table(self.analysis_table, self.analysis_data)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

            try:
                if self._ensure_post_ui():
                    cfg = IRMSPostConfig()
                    self.post_results, self.qc_summary, self.batch_summary = postprocess_irms(self.analysis_data, cfg)
                    self._sync_post_tab_visibility(); self._update_postprocess_view()
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

            try: self.update_plot_configs()
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

            set_status(self.status_label,"Status: IRMS DI calibrated.", "success")
        except Exception as e:
            logging.error(f"DI processing failed: {e}", exc_info=True)
        return


    def _run_ea_pipeline(self, _std_for_processing, measured_order_default):
        """IRMS (EA) linearity / drift / calibration pipeline."""
        # 0) START FROM RAW, NOT CURRENT ANALYSIS
        raw = getattr(self, "injection_data", None)
        if raw is None or not isinstance(raw, pd.DataFrame) or raw.empty:
            raw = getattr(self, "data", None)
        if raw is None or not isinstance(raw, pd.DataFrame) or raw.empty:
            QMessageBox.warning(self, "No Data", "Load an EA file first (Raw should be visible).")
            return

        # 1) Determine the *current* peak selection
        def _current_peak():
            # UI combo (if present)
            for attr in ("ea_peak_combo", "ea_peak_combo_analysis", "_ea_single_combo"):
                cb = getattr(self, attr, None)
                if cb is not None:
                    try:
                        t = cb.currentText()
                        if t and str(t).strip():
                            return int(float(t))
                    except Exception as e:

                        logging.warning(f"Exception caught: {e}")
            # Config (if you run config-driven peak)
            gp = getattr(self, "_get_peak_from_config", None)
            if callable(gp):
                try:
                    iso = getattr(self, "current_isotope", None) or "d13C"
                    v = gp(iso)
                    if v is not None:
                        return int(v)
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")
            # Fallback: infer from current Analysis (if exists) or RAW first peak
            try:
                ana = getattr(self, "analysis_data", None)
                if isinstance(ana, pd.DataFrame) and not ana.empty:
                    for c in ("Peak", "peak"):
                        if c in ana.columns:
                            s = pd.to_numeric(ana[c], errors="coerce").dropna().astype(int)
                            vals = s.unique().tolist()
                            if len(vals) == 1:
                                return int(vals[0])
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            for c in ("Peak", "peak"):
                if c in raw.columns:
                    try:
                        s = pd.to_numeric(raw[c], errors="coerce").dropna().astype(int)
                        opts = sorted(set(s.tolist()))
                        if opts:
                            return int(opts[0])
                    except Exception as e:

                        logging.warning(f"Exception caught: {e}")
            return None

        peak = _current_peak()

        # 2) Re-filter RAW → Analysis by Peak (fresh subset)
        ana = raw.copy()
        peak_col = None
        for c in ("Peak", "peak"):
            if c in raw.columns:
                peak_col = c
                break

        if (peak is not None) and peak_col:
            try:
                vals = pd.to_numeric(raw[peak_col], errors="coerce")
                sub = raw.loc[vals == float(peak)].copy()
                if not sub.empty:
                    ana = sub
            except Exception:
                ana = raw.copy()

        # 3) DROP previously computed/corrected columns (reset state)
        def _strip_computed(df: pd.DataFrame) -> pd.DataFrame:
            # Remove typical computed/corrected columns from previous runs
            suffixes = (
                "_lin_corrected", "_drift_corrected", "_lin_drift_corrected",
                "_memory_corrected", "_corrected", "_calibrated",
                "_std", "_stderr", "_sd", "_fit", "_resid"
            )
            prefixes = ("cal_", "fit_", "diag_", "qc_", "post_")
            keep = []
            for col in df.columns:
                sc = str(col)
                if any(sc.endswith(suf) for suf in suffixes):
                    continue
                if any(sc.startswith(pre) for pre in prefixes):
                    continue
                keep.append(col)
            return df.loc[:, keep].copy()

        ana = _strip_computed(ana)

        # Live pointers reflect the re-filtered, reset subset
        self.data = raw.copy()
        self.injection_data = self.data
        self.analysis_data = ana.copy()

        # 4) Now run linearity / drift / calibration on the fresh subset
        working = self.analysis_data.copy(deep=True)

        # Make IDs safe
        if "sample_id" in working.columns:
            working["sample_id"] = working["sample_id"].astype(str).str.strip()
        if "Analysis" in working.columns:
            try:
                working["Analysis"] = pd.to_numeric(working["Analysis"], errors="coerce")
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        # CN aliasing if available
        try:
            working = resolve_columns(working, instrument=ResolverInstrument.IRMS_EA)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # Options
        try: lin_mode = self.ea_linearity_combo.currentText()
        except Exception: lin_mode = "Off"
        try: drift_mode = self.ea_drift_combo.currentText()
        except Exception: drift_mode = "Off"
        try: robust = bool(self.ea_robust_check.isChecked())
        except Exception: robust = False

        # Helpers to attach corrected series aligned to 'working'
        def _to_series(x, *, name=None, index=None):
            if isinstance(x, pd.DataFrame):
                if x.shape[1] == 1:
                    s = x.iloc[:, 0]
                elif name and name in x.columns:
                    s = x[name]
                else:
                    try:
                        s = x.squeeze("columns")
                        if not isinstance(s, pd.Series):
                            s = pd.Series(np.nan, index=index)
                    except Exception:
                        s = pd.Series(np.nan, index=index)
            elif isinstance(x, pd.Series):
                s = x
            else:
                s = pd.Series(x, index=index)
            if index is not None and not s.index.equals(index):
                s = s.reindex(index)
            return pd.to_numeric(s, errors="coerce")

        def _attach(colname, series_like):
            workingcolname = _to_series(series_like, name=colname, index=working.index)

        # Standards mask (optional)
        try:
            from isotope_processor import _default_std_mask
            std_mask = _default_std_mask(working)
        except Exception:
            std_mask = pd.Series(False, index=working.index)

        # Detect EA isotopes present in the (fresh) subset
        ea_isos = [nm for nm in ("d13C", "d15N") if any(str(c).startswith(nm) for c in working.columns)]
        if not ea_isos:
            for c in working.columns:
                cl = str(c).lower()
                if "15n" in cl and "/" in cl and "14" in cl and "d15N" not in ea_isos:
                    ea_isos.append("d15N")
                if "13c" in cl and "/" in cl and "12" in cl and "d13C" not in ea_isos:
                    ea_isos.append("d13C")

        # 1) LINEARITY
        extra_stage_sd = {}
        try:
            from isotope_processor import fit_linearity_ea, apply_linearity_ea
        except Exception:
            fit_linearity_ea = apply_linearity_ea = None

        for nm in ea_isos:
            try:
                if fit_linearity_ea and isinstance(lin_mode, str) and lin_mode.lower() in ("linear", "quadratic"):
                    fit_lin = fit_linearity_ea(
                        working, nm,
                        amount_col=("Amount" if "Amount" in working.columns else "Area"),
                        std_mask=std_mask,
                        model=lin_mode.lower(),
                        robust=robust
                    )
                    corrected, sd_lin, _ = apply_linearity_ea(working, nm, fit_lin)
                    _attach(f"{nm}_lin_corrected", corrected)
                else:
                    sd_lin = float("nan")
            except Exception:
                sd_lin = float("nan")
            extra_stage_sdnm = 0.0 if pd.isna(sd_lin) else float(sd_lin)

        # 2) DRIFT
        try:
            from isotope_processor import fit_drift_ea, apply_drift_ea
        except Exception:
            fit_drift_ea = apply_drift_ea = None

        for nm in ea_isos:
            try:
                if fit_drift_ea and isinstance(drift_mode, str) and drift_mode.lower().startswith("linear"):
                    axis = "time" if "time" in drift_mode.lower() else "order"
                    fit_dr = fit_drift_ea(working, nm, axis=axis, std_mask=std_mask, robust=robust)
                    corrected, sd_dr, _ = apply_drift_ea(working, nm, fit_dr)
                    _attach(f"{nm}_lin_drift_corrected", corrected)
                else:
                    sd_dr = float("nan")
            except Exception:
                sd_dr = float("nan")
            s2 = (extra_stage_sd.get(nm, 0.0) ** 2) + (0.0 if pd.isna(sd_dr) else float(sd_dr) ** 2)
            extra_stage_sdnm = s2 ** 0.5

        # 3) CALIBRATION (on the fresh subset)
        std_df = getattr(self, "standards_data", None)
        try:
            from isotope_processor import DEFAULT_MEASURED_ORDER
        except Exception:
            DEFAULT_MEASURED_ORDER = ("_lin_drift_corrected", "_lin_corrected",
                                    "_memory_corrected", "_drift_corrected", "_corrected", "")

        def _meas_order_for(nm):
            return (f"{nm}_lin_drift_corrected", f"{nm}_lin_corrected") + tuple(DEFAULT_MEASURED_ORDER)

        iso_tuple = tuple(i for i in ea_isos if any(str(c).startswith(i) for c in working.columns))
        extra_unc = {nm: float(extra_stage_sd.get(nm, 0.0) or 0.0) for nm in iso_tuple}

        try:
            calibrated, fits = apply_calibration_generic(
                working,
                std_df,
                id_col="sample_id",
                isotopes=iso_tuple,
                measured_order=tuple(o for nm in iso_tuple for o in _meas_order_for(nm)),
                extra_stage_unc=extra_unc,
            )
        except Exception as e:
            logging.error(f"EA calibration failed: {e}", exc_info=True)
            calibrated, fits = working.copy(), {}

        # Re-apply EXACT same subset key (sample_id + Peak) to the calibrated result
        try:
            current = self.analysis_data  # after reset above, this is the same as 'working'
            peak_col_cal = "Peak" if "Peak" in current.columns else ("peak" if "peak" in current.columns else None)
            if peak_col_cal and peak_col_cal != "Peak":
                current = current.rename(columns={peak_col_cal: "Peak"})
                peak_col_cal = "Peak"

            key_cols = []
            if "sample_id" in current.columns:
                key_cols.append("sample_id")
            if peak_col_cal and peak_col_cal in current.columns:
                key_cols.append(peak_col_cal)

            if key_cols:
                keep_keys2 = current[key_cols].drop_duplicates().copy()
                out = calibrated.merge(keep_keys2, on=key_cols, how="inner")
            else:
                out = calibrated.copy()
            self.analysis_data = out
        except Exception:
            self.analysis_data = calibrated.copy()

        self.calibration_fits = fits

        # Post + repaint
        try:
            from isotope_processor import IRMSPostConfig, postprocess_irms
            if self._ensure_post_ui():
                cfg = IRMSPostConfig()
                self.post_results, self.qc_summary, self.batch_summary = postprocess_irms(self.analysis_data, cfg)
                self._sync_post_tab_visibility(); self._update_postprocess_view()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        try: self.update_data_table(self.injection_table, self.injection_data)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try: self.update_data_table(self.analysis_table, self.analysis_data)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try: self.update_plot_configs()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        set_status(self.status_label,"Status: IRMS EA corrected & calibrated.", "success")
        return

    def _run_laser_pipeline(self, _std_for_processing, measured_order_default):
        """Laser (LGR / Picarro) memory / drift / calibration pipeline."""
        instrument_label = self.instrument_combo.currentText()
        if not all([self.data_file, _std_for_processing is not None]):
            QMessageBox.warning(self, "Missing Files", "Please load data and standards files first.")
            return

        roles_from_gui = self.get_roles_from_table()
        if roles_from_gui is None:
            return

        # Selected isotopes (robust fallback)
        try:
            isotopes_to_process = self._selected_isotopes_for_laser()
        except Exception:
            cols = set(getattr(self, "data", pd.DataFrame()).columns)
            isotopes_to_process = [i for i in ("d18O", "dD", "d17O") if i in cols]
        if not isotopes_to_process:
            QMessageBox.warning(self, "No Isotopes", "Please select at least one isotope to process.")
            return

        # ── Pre-processing role validation ─────────────────────────
        # Identification was done by database.get_batch_data() at
        # load time.  We only validate here.
        try:
            _val_issues = validate_laser_roles(
                roles              = roles_from_gui,
                injection_df = getattr(self, "data", pd.DataFrame()),
                standards_df       = getattr(self, "standards_data", pd.DataFrame()),
                correction_methods = {
                    "linearity": self._get_combo_text("linearity_combo", "None"),
                    "drift":     self._get_combo_text("drift_combo",     "None"),
                    "memory":    self._get_combo_text("memory_combo",    "None"),
                },
                instrument = instrument_label,
                isotope    = (isotopes_to_process[0] if isotopes_to_process else "d18O"),
            )
        except Exception as _ve:
            logging.warning(f"Role validation error: {_ve}", exc_info=True)
            _val_issues = []

        _errors   = [i for i in _val_issues if i.severity == Severity.ERROR]
        _warnings = [i for i in _val_issues if i.severity == Severity.WARNING]

        if _errors:
            QMessageBox.critical(
                self, "Role Assignment Errors — Cannot Process",
                "Resolve the following before processing:\n\n"
                + format_issues_for_dialog(_errors),
            )
            return

        if _warnings:
            if QMessageBox.warning(
                self, "Role Assignment Warnings",
                format_issues_for_dialog(_warnings) + "\n\nContinue anyway?",
                QMessageBox.Yes | QMessageBox.No,
            ) != QMessageBox.Yes:
                return
        # ── end role validation ────────────────────────────────────"""
        try:
            self.config.outlier_threshold = float(self.outlier_factor_edit.text())
        except Exception:
            self.config.outlier_threshold = 2.0

        correction_methods = {
            "linearity": self._get_combo_text("linearity_combo", "Off"),
            "memory":    self._get_combo_text("memory_combo",    "Fast/Slow Pool Carryover"),
            "drift":     self._get_combo_text("drift_combo",     "Linear"),
            "mwl":       self._get_combo_text("mwl_combo",       "GMWL_Craig"),
        }

        # --- Memory selection logic (only intra-sample needs an Analysis ID) ---
        mem_txt = (self.memory_combo.currentText() or "").strip().lower()
        needs_analysis = any(k in mem_txt for k in ("intra", "per analysis", "Exponential", "Asymptotic"))

        analysis_id_for_memory = 0  # convention: 0 = standards-characterized memory
        if needs_analysis:
            # try to read the currently selected Analysis from the Analysis table
            analysis_id_for_memory = None
            try:
                t = getattr(self, "analysis_table", None)
                if t and t.selectedItems():
                    # find "Analysis" column
                    col_idx = -1
                    for c in range(t.columnCount()):
                        hdr = t.horizontalHeaderItem(c)
                        if hdr and hdr.text().strip().lower() == "analysis":
                            col_idx = c; break
                    if col_idx != -1:
                        row = t.selectedItems()[0].row()
                        txt = t.item(row, col_idx).text().strip()
                        if txt:
                            analysis_id_for_memory = int(float(txt))
            except Exception:
                analysis_id_for_memory = None

            if analysis_id_for_memory is None:
                # auto-pick a sensible default from the current analysis/raw data
                picked = None
                for nm in ("analysis_data", "injection_data", "data"):
                    df = getattr(self, nm, None)
                    if isinstance(df, pd.DataFrame) and "Analysis" in df.columns and not df.empty:
                        s = pd.to_numeric(df["Analysis"], errors="coerce").dropna()
                        if not s.empty:
                            # mode tends to correspond to the dominant/current analysis
                            picked = int(s.mode().iloc[0])
                            break
                if picked is None:
                    # final fallback: avoid blocking the run — use first visible number or 0
                    picked = 0
                analysis_id_for_memory = picked
        # Store for downstream/plots (your plots use 0 for standards-characterized)
        self.memory_analysis_id = analysis_id_for_memory
        correction_methods["memory_analysis_id"] = analysis_id_for_memory

        include_ignored_flag = False
        try:
            include_ignored_flag = bool(self.include_ignored_check.isChecked())
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        self.process_button.setEnabled(False)
        set_status(self.status_label,"Status: Processing...", "processing")

        instrument = InstrumentType(instrument_label)
        corrections_to_apply = ['memory', 'drift', 'validation']
        if self.outlier_method_combo.currentText() != "None":
            corrections_to_apply.insert(0, 'outlier')

        # Pick the best “already-prepared” DF if we’re in LIMS flow
        pre_df = None
        for nm in ("injection_data", "data"):
            df = getattr(self, nm, None)
            if isinstance(df, pd.DataFrame) and not df.empty:
                # Heuristic: LIMS-enriched frames have either PositionInRun|run_order or AnalysisID
                has_lims_keys = any(c in df.columns for c in ("PositionInRun", "run_order", "AnalysisID"))
                if has_lims_keys:
                    pre_df = df.copy()
                    break

        # Delegate to ViewModel
        self.viewmodel.start_processing(
            data_file=(None if pre_df is not None else self.data_file),
            standards_data=_std_for_processing,
            instrument=instrument,
            config=self.config,
            corrections=corrections_to_apply,
            roles=roles_from_gui,
            methods=correction_methods,
            isotopes=isotopes_to_process,
            include_ignored=include_ignored_flag,
            preloaded_df=pre_df
        )
        # After worker: apply generic calibration + update UI
        def _after_worker(payload_injection, payload_analysis, _validation_unused, memory_fits, drift_fits, *args):
            try:
                self.memory_fits = memory_fits or {}
                self.drift_fits  = drift_fits  or {}
                base = payload_analysis if isinstance(payload_analysis, pd.DataFrame) else getattr(self, "data", pd.DataFrame())
                base = base.copy()
                base = resolve_columns(base, instrument=instrument)
                # choose isotopes present
                try:
                    chosen = []
                    for chk, iso in [(getattr(self, "d18O_check", None), "d18O"),
                                    (getattr(self, "dD_check",   None), "dD"),
                                    (getattr(self, "d17O_check", None), "d17O"),
                                    (None, "d13C"), (None, "d15N")]:
                        if chk is None:
                            if any(str(c).startswith(iso) for c in base.columns):
                                chosen.append(iso)
                        elif chk.isChecked():
                            chosen.append(iso)
                    isotopes_to_use = tuple(sorted(set(chosen)))
                except Exception:
                    bases = ("d18O","dD","d17O","d13C","d15N")
                    isotopes_to_use = tuple(i for i in bases if any(str(c).startswith(i) for c in base.columns))

                calibrated, fits = apply_calibration_generic(
                    base,
                    _std_for_processing,
                    id_col="sample_id",
                    isotopes=isotopes_to_use,
                    measured_order=measured_order_default,
                    extra_stage_unc=None,  # avoid double-counting; worker already produced per-row {iso}_u
                )

                self.analysis_data = resolve_columns(calibrated, instrument=instrument)
                self.calibration_fits = fits or {}
                self._analysis_full = self.analysis_data.copy(deep=True)
                # tables
                self._populate_isotope_selector_from(self.analysis_data)
                self._refresh_tables_for_active_isotope()
                self.update_data_table(self.injection_table, self.injection_data)
                self.update_data_table(self.analysis_table, self.analysis_data)

                # plots
                self.update_plot_configs()
                host = self._tab_host()
                try:
                    if host and hasattr(self, "plot_tab"):
                        idx = host.indexOf(self.plot_tab)
                        if idx != -1:
                            host.setTabEnabled(idx, True)
                            host.setCurrentWidget(self.plot_tab)
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")

                # validation summary on Plots tab
                try:
                    roles_from_gui2 = self.get_roles_from_table() or {}
                    z_thr = float(getattr(self.config, "validation_z", 2.0) or 2.0)
                    val_df, val_summary = compute_validation_results(
                        self.analysis_data, _std_for_processing,
                        roles_from_gui=roles_from_gui2,
                        id_col="sample_id",
                        isotopes=isotopes_to_use,
                        z_threshold=z_thr,
                    )
                    self.validation_results = val_df
                    if hasattr(self, "_set_validation_status"):
                        self._set_validation_status(val_summary or "Validation computed.")
                except Exception as e:
                    logging.debug(f"Validation summary skipped: {e}")

                # compact log
                try:
                    parts = []
                    for iso, f in (self.calibration_fits or {}).items():
                        a = f.get("slope"); b = f.get("intercept"); r2 = f.get("r2"); n = f.get("n")
                        mcol = f.get("meas_col"); tcol = f.get("true_col")
                        parts.append(f"{iso}[n={n}] slope={a:.6g}, intercept={b:.6g}, R2={r2:.3f} ({mcol} -> {tcol})")
                    logging.info("Calibration fits: " + (" | ".join(parts) if parts else "none"))
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")

                set_status(self.status_label,"Status: Laser data processed & calibrated.", "success")
                if hasattr(self, "_schedule_auto_plot"):
                    self._schedule_auto_plot(0)

            except Exception as e:
                logging.error(f"Post-worker calibration error: {e}", exc_info=True)
                QMessageBox.critical(self, "Calibration Error", str(e))
            finally:
                try:
                    self.process_button.setEnabled(True)
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")

        try:
            self.viewmodel.processingFinished.disconnect()
        except Exception:
            pass
        self.viewmodel.processingFinished.connect(_after_worker)
        self.viewmodel.processingFinished.connect(self.on_processing_finished)
        # self.thread.start() is now handled by viewmodel.start_processing

           
    def on_processing_finished(self, injection_data, analysis_data, validation_results, memory_fits, drift_fits, mem_factors):
        self.injection_data = injection_data
        self.analysis_data = analysis_data
        self.validation_results = validation_results
        self.memory_fits = memory_fits or {}
        self.drift_fits  = drift_fits  or {}
        self.memory_factors = mem_factors or {}
        
        # optional: log for sanity
        try:
            logging.info("Memory fits: %s | Drift fits: %s",
                        list(self.memory_fits.keys()), list(self.drift_fits.keys()))
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        
        # --- new: collect & log calibration fits from the worker ---
        try:
            fits = getattr(self.worker, "calibration_fits", None)
            if isinstance(fits, dict) and fits:
                # keep around for plotting/exports
                self.calibration_fits = fits
                try:
                    if self.calibration_fits:
                        for iso, f in self.calibration_fits.items():
                            logging.info(f"Calibration [{iso}]: slope={f.get('slope'):.6f}, intercept={f.get('intercept'):.3f}, "
                                        f"r2={f.get('r2'):.4f}, n={f.get('n')}, sd_resid={f.get('sd_resid')}")
                    else:
                        logging.info("Calibration: no fits available.")
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")
                
                def _fmt(x, fmt="g"):
                    try:
                        return format(float(x), fmt)
                    except Exception as e:

                        logging.warning(f"Exception caught: {e}"); return "NA"

                parts = []
                for iso, f in fits.items():
                    slope = _fmt(f.get("slope"), " .6g")
                    intercept = _fmt(f.get("intercept"), " .6g")
                    r2 = _fmt(f.get("r2"), ".3f")
                    n = f.get("n", "NA")
                    mcol = f.get("meas_col", "")
                    tcol = f.get("true_col", "")
                    # ASCII only to avoid encoding errors in some terminals
                    parts.append(f"{iso}[n={n}] slope={slope}, intercept={intercept}, R^2={r2} ({mcol} -> {tcol})")

                logging.info("Calibration fits: " + " | ".join(parts))
            else:
                logging.info("Calibration fits: none")
        except Exception as e:
            logging.debug(f"Calibration fits logging skipped: {e}")     
        
        # NEW: validation summary log line
        self._log_validation_summary(validation_results)        
        self.update_data_table(self.injection_table, self._view_for_isotope(self.injection_data, self.current_isotope))
        self.update_data_table(self.analysis_table, self._view_for_isotope(self.analysis_data, self.current_isotope))
        self.output_tabs.setTabText(0, "Processed Injection Data")

        self.update_plot_configs()
        self.output_tabs.setCurrentWidget(self.plot_tab); set_status(self.status_label,"Status: Processing Complete", "success")
        self.process_button.setEnabled(True); 
        # QMessageBox.information(self, "Success", "Data processing completed successfully.")

        try:
            roles_from_gui = self.get_roles_from_table() or {}
        except Exception:
            roles_from_gui = {}
        # decide isotopes present
        iso_candidates = [i for i in ("d18O","dD","d17O","d13C","d15N") if f"{i}_calibrated" in self.analysis_data.columns]
        # (B) inject z-score columns for validation sample(s)
        self.analysis_data = self._inject_validation_zscores(self.analysis_data, getattr(self, "standards_data", None), roles_from_gui)

        from isotope_processor import compute_validation_results  # ensure imported
        val_df, val_summary = compute_validation_results(
            self.analysis_data,
            getattr(self, "standards_data", None),
            roles_from_gui=roles_from_gui,
            id_col="sample_id",
            isotopes=tuple(iso_candidates),
            z_threshold=float(getattr(self.config, "validation_z", 2.0) or 2.0)
        )
        self.validation_results = val_df
        # Make sure this call is *after* update_plot_configs()
        self._set_validation_status(val_summary)
        self._update_save_to_db_enabled()
        
        if hasattr(self, "action_export_csv"):
            self.action_export_csv.setEnabled(True)
                        
    def on_processing_error(self, error_message):
        set_status(self.status_label,"Status: Error", "error"); self.process_button.setEnabled(True)
        # QMessageBox.critical(self, "Processing Error", f"An error occurred: {error_message}")

    def _update_postprocess_view(self):
        """Refresh Results / QC / Batch tables for active isotope."""
        iso = getattr(self, "current_isotope", None)
        iso_n = _iso_norm(iso)

        # Results table (use your existing per-iso narrow view if present)
        if isinstance(self.post_results, pd.DataFrame):
            res_view = self._view_for_isotope(self.post_results, iso) if iso else self.post_results
            self.update_data_table(self.post_results_table, res_view)
        else:
            self.update_data_table(self.post_results_table, pd.DataFrame())

        # QC: filter by 'isotope' if present
        if isinstance(self.qc_summary, pd.DataFrame):
            qc_view = self.qc_summary.copy()
            if "isotope" in qc_view.columns and iso:
                qc_view = qc_view[qc_view["isotope"].astype(str).map(_iso_norm) == iso_n]
            self.update_data_table(self.qc_table, qc_view)
        else:
            self.update_data_table(self.qc_table, pd.DataFrame())

        # Batch summary
        if isinstance(self.batch_summary, pd.DataFrame):
            bs_view = self.batch_summary.copy()
            if "isotope" in bs_view.columns and iso:
                bs_view = bs_view[bs_view["isotope"].astype(str).map(_iso_norm) == iso_n]
            self.update_data_table(self.batch_summary_table, bs_view)
        else:
            self.update_data_table(self.batch_summary_table, pd.DataFrame())

    def _tab_host(self):
        return getattr(self, "output_tabs", None) or getattr(self, "tabs", None)

    def _ensure_post_ui(self) -> bool:
        """Create the Post-Processing tab (tables) once."""
        
        host = self._tab_host()
        if host is None:
            return False
        # If already created, just return
        if hasattr(self, "post_tab") and self.post_tab is not None:
            return True
        self.post_tab = QWidget(self)
        lay = QVBoxLayout(self.post_tab)
        self.post_results_table = QTableWidget(self.post_tab)
        self.qc_table           = QTableWidget(self.post_tab)
        self.batch_summary_table= QTableWidget(self.post_tab)
        lay.addWidget(QLabel("Merged Results (by sample_id)", self.post_tab))
        lay.addWidget(self.post_results_table)
        lay.addWidget(QLabel("Standards QC (residuals & z)", self.post_tab))
        lay.addWidget(self.qc_table)
        lay.addWidget(QLabel("Batch Summary (per isotope)", self.post_tab))
        lay.addWidget(self.batch_summary_table)
        idx = host.addTab(self.post_tab, "Post-Processing")
        # Don’t show by default; we’ll toggle in _sync_post_tab_visibility
        host.setTabEnabled(idx, False)
        return True

    def reset_postprocess(self, hide_tab: bool = True):
        """Clear Post-Processing data & optionally hide its tab."""
        self.post_results = pd.DataFrame()
        self.qc_summary = pd.DataFrame()
        self.batch_summary = pd.DataFrame()
        if hasattr(self, "post_results_table"):
            self.update_data_table(self.post_results_table, pd.DataFrame())
        if hasattr(self, "qc_table"):
            self.update_data_table(self.qc_table, pd.DataFrame())
        if hasattr(self, "batch_summary_table"):
            self.update_data_table(self.batch_summary_table, pd.DataFrame())
        if hide_tab:
            host = self._tab_host()
            if host is not None and hasattr(self, "post_tab"):
                idx = host.indexOf(self.post_tab)
                if idx != -1:
                    host.setTabEnabled(idx, False)

    def export_results_unified(self):
        """
        Export the relevant processed outputs for the current instrument:
        - IRMS: post_results, qc_summary, batch_summary
        - LGR/Picarro: analysis_data
        """
       
        # Pick a folder
        folder = QFileDialog.getExistingDirectory(self, "Choose export folder")
        if not folder:
            return
        try:
            label = self.instrument_combo.currentText()
        except Exception:
            label = ""
        wrote_any = False
        try:
            if label in ("IRMS (EA)", "IRMS (Thermo DI)"):
                if isinstance(self.post_results, pd.DataFrame) and not self.post_results.empty:
                    self.post_results.to_csv(os.path.join(folder, "IRMS_post_results.csv"), index=False)
                    wrote_any = True
                if isinstance(self.qc_summary, pd.DataFrame) and not self.qc_summary.empty:
                    self.qc_summary.to_csv(os.path.join(folder, "IRMS_qc_summary.csv"), index=False)
                    wrote_any = True
                if isinstance(self.batch_summary, pd.DataFrame) and not self.batch_summary.empty:
                    self.batch_summary.to_csv(os.path.join(folder, "IRMS_batch_summary.csv"), index=False)
                    wrote_any = True
            else:
                # Laser instruments: export analysis_data (what your Analysis tab shows)
                if isinstance(self.analysis_data, pd.DataFrame) and not self.analysis_data.empty:
                    self.analysis_data.to_csv(os.path.join(folder, "analysis_data.csv"), index=False)
                    wrote_any = True
            if not wrote_any:
                QMessageBox.information(self, "Nothing to Export", "No processed data available to export.")
            else:
                QMessageBox.information(self, "Export Complete", f"Files written to:\n{folder}")

        except Exception as e:
            logging.exception("Export failed")
            QMessageBox.critical(self, "Export Error", str(e))

    def _refresh_tables_for_active_isotope(self):
        
        iso = getattr(self, "current_isotope", None)

        # --- RAW tab (per-isotope)
        try:
            raw_df = self._raw_df_for_isotope(iso)
            self.update_data_table(self.injection_table, raw_df if isinstance(raw_df, pd.DataFrame) else pd.DataFrame())
        except Exception:
            self.update_data_table(self.injection_table, pd.DataFrame())

        # --- Analysis tab (filter analysis_data by isotope if present)
        try:
            if isinstance(getattr(self, "analysis_data", None), pd.DataFrame) and not self.analysis_data.empty:
                view = self._view_for_isotope(self.analysis_data, iso)
                self.update_data_table(self.analysis_table, view)
            else:
                self.update_data_table(self.analysis_table, pd.DataFrame())
        except Exception:
            self.update_data_table(self.analysis_table, pd.DataFrame())

    def _is_identifier_col(self, col: str) -> bool:
        c = str(col).strip().lower()
        return (
            c in {"identifier 1", "identifier", "identifier1",
                  "sample_id", "sample_id_num", "sample_code", "sample_label",
                  "sample_name", "sample name", "name", "id",
                  "prefix", "ref_name", "ref prefix", "ref sample name",
                  "role", "role_code", "rolecode",
                  "instrument_id", "instrumentanalysisid", "instrument analysis id", "ourlabid",
                  "remarks", "mwl_applied", "sample_type", "sample_type_name",
                  "timestamp", "date", "datetime", "date-time analyzed", "time",
                  "vial_label", "vial label", "tray_vial_position"}
            or c.startswith("identifier")
            or c.startswith("is_")        # is_linearity_id, is_drift_id, etc.
            or c.endswith("_id")          # analysis_id, sample_id, etc.
            or c.endswith("_name")        # sample_name, equipment_name, etc.
        )

    def _intish_columns(self) -> set:
        # any of these should *display* as integers
        return {
            "block_no", "line", "peak", "peak nr", "peak no",
            "injection_no", "cumulative_injection",
            "tray_vial_position", "tray position", "vial position", "tray_vial_pos",
            "analysis"  # logical counter; often int
        }

    def _stringify_identifier(self, v) -> str:
        # never let identifiers show as "1.0"
        if v is None:
            return ""
        s = str(v).strip()
        # Only try float() for values that genuinely look numeric
        if s and s.replace('.','',1).replace('-','',1).replace('e','',1).replace('E','',1).replace('+','',1).isdigit():
            try:
                f = float(s)
                if f == int(f) and not (f != f):
                    return str(int(f))
                return s
            except Exception:
                pass
        return s

    def _format_num_for_display(self, col: str, val) -> str:
        
        if pd.isna(val):
            return ""
        c = str(col).strip().lower()

        # identifiers always as clean text
        if self._is_identifier_col(col):
            return self._stringify_identifier(val)

        # force selected columns to integer looking strings
        if c in self._intish_columns():
            try:
                return f"{int(float(val))}"
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); return self._stringify_identifier(val)

        # scientific notation for water_conc
        if c == "water_conc":
            try:
                return f"{float(val):.3e}"
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); return str(val)

        # isotope-ish numbers (and their *_calibrated/_corrected/_excess/_u)
        if any(k in c for k in ["d13", "d15", "d18", "d17", "δ", "delta", "_calibrated", "_corrected", "_excess", "_u", "_sd", "_sem"]):
            try:
                return f"{float(val):.3f}"
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); return str(val)

        # default: only attempt float formatting on genuinely numeric values
        if isinstance(val, (int, float)):
            try:
                return f"{float(val):.6g}"
            except Exception:
                pass
        # numpy scalars
        if hasattr(val, "dtype"):
            try:
                return f"{float(val):.6g}"
            except Exception:
                pass
        return str(val)

    def _normalize_block_no(self, df):
        
        if not isinstance(df, pd.DataFrame) or df.empty:
            return df
        if "block_no" in df.columns:
            df = df.copy()
            df["block_no"] = pd.to_numeric(df["block_no"], errors="coerce").astype("Int64")
        return df

    def _order_analysis_columns(self, df):
        # move timestamp, cumulative_injection, water_conc to the end for the Analysis tab
        if df is None or df.empty:
            return df
        cols = list(df.columns)
        tail = []
        for key in [ "line","seconds", "sample_id", "role", "analysisid", "timestamp", "cumulative_injection", "water_conc"]:
            for c in cols:
                if c.lower() == key and c not in tail:
                    tail.append(c)
                    break
        head = [c for c in cols if c not in tail]
        
        return df[head + tail]

    def _order_injection_columns(self, df):
        # move timestamp, cumulative_injection, water_conc to the end for the Analysis tab
        if df is None or df.empty:
            return df
        cols = list(df.columns)
        head = []
        for key in ["analysisid"]:
            for c in cols:
                if c.lower() == key and c not in head:
                    head.append(c)
                    break
        tail = [c for c in cols if c not in head]
        return df[head + tail]
    # --- main renderer ---------------------------------------------------------

    def update_data_table(self, table, data):
        """Render a DataFrame into a QTableWidget with robust formatting and z-score tinting."""
        
        def _tint_for_z(z):
            try:
                z = float(z)
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); return None
            az = abs(z)
            if az <= 2:
                return QColor(221, 245, 221)   # green-ish
            if az <= 3:
                return QColor(255, 243, 205)   # amber-ish
            return QColor(255, 221, 221)       # red-ish

        df = data.copy() if isinstance(data, pd.DataFrame) else pd.DataFrame()
        df = self._normalize_block_no(df)

        # If this is the Analysis table, reorder a few columns to the end (your helper)
        if table is getattr(self, "analysis_table", None) and not df.empty:
            df = self._order_analysis_columns(df)
            
        if table is getattr(self, "injection_table", None) and not df.empty:
            df = self._order_injection_columns(df)
            
        table.blockSignals(True)
        try:
            table.setUpdatesEnabled(False)
            table.clear()

            if df.empty:
                table.setRowCount(0)
                table.setColumnCount(0)
                return

            headers = [str(c) for c in df.columns]
            table.setColumnCount(len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setRowCount(len(df))

            # Precompute which columns are z-score columns
            z_cols = {i for i, h in enumerate(headers) if h.endswith("_z")}

            for r_idx, (_, row) in enumerate(df.iterrows()):
                for c_idx, col in enumerate(headers):
                    val = row[col]
                    text = self._format_num_for_display(col, val)
                    item = QTableWidgetItem(text)

                    # Apply background tint for z-score columns
                    if c_idx in z_cols:
                        color = _tint_for_z(val)
                        if color is not None:
                            item.setBackground(color)

                    table.setItem(r_idx, c_idx, item)

            table.resizeColumnsToContents()
        finally:
            table.setUpdatesEnabled(True)
            table.blockSignals(False)


    def _normalize_id(self, s):
        if s is None:
            return None
        s = str(s)
        # IRMS often has "ID/replicate" — use the first token, match your data pipeline
        return s.split("/")[0].strip()

    def _current_sample_ids(self):
        """
        Return a set of normalized sample_ids currently present in self.data.
        Falls back to ref_name if sample_id not found.
        """
        try:
            df = getattr(self, "data", None)
            if df is None or df.empty:
                return set()
            cols = [c for c in df.columns]
            sid_col = next((c for c in cols if str(c).lower().strip() == "sample_id"), None)
            if sid_col is not None:
                return {self._normalize_id(v) for v in df[sid_col].astype(str).tolist()}
            # fall back to ref_name if needed
            r_col = next((c for c in cols if str(c).lower().strip() == "ref_name"), None)
            if r_col is not None:
                return {self._normalize_id(v) for v in df[r_col].astype(str).tolist()}
            return set()
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return set()

    def _standards_to_gui_table(self, std_long_df, role_map=None):
        """
        Convert flexible long standards to the wide GUI table expected by update_standards_table:
        • Filters to current data's sample_ids (if available)
        • Adds display columns for isotope true values: d18O, dD, d17O, d13C, d15N, ...
        • Adds uncertainties: {iso}_uncertainty (optional, if present in input)
        • Adds role flags: is_calibration_id, is_control_id, is_linearity_id, is_memory_id, is_drift_id, is_blank_id
        NOTE: We DO NOT include the '{iso}_true' duplicate columns in the returned frame (to avoid duplicates in UI).
            The calibration still uses the long standards (self.standards_data) with true/uncertainty internally.
        """
        
        if std_long_df is None or (hasattr(std_long_df, "empty") and std_long_df.empty):
            return pd.DataFrame(columns=[
                "sample_id", "ref_name",
                "is_calibration_id", "is_control_id", "is_linearity_id", "is_memory_id", "is_drift_id", "is_blank_id"
            ])

        df = std_long_df.copy()
        df.columns = [str(c).strip() for c in df.columns]

        # Ensure sample_id/ref_name exist
        if "sample_id" not in df.columns and "ref_name" in df.columns:
            df["sample_id"] = df["ref_name"].astype(str)
        if "ref_name" not in df.columns and "sample_id" in df.columns:
            df["ref_name"] = df["sample_id"].astype(str)

        # Normalize key & filter to dataset ids if present
        df["_key_"] = df["sample_id"].astype(str).map(self._normalize_id)
        present_ids = self._current_sample_ids()
        if present_ids:
            df = df[df["_key_"].isin(present_ids)]

        base = df.groupby("_key_", as_index=False).agg({
            "sample_id": "first",
            "ref_name": "first"
        })

        # Pivot true & uncertainty to wide
        has_iso_info = {"isotope", "true"}.issubset(set(df.columns))
        if has_iso_info:
            df["true"] = pd.to_numeric(df["true"], errors="coerce")
            if "uncertainty" in df.columns:
                df["uncertainty"] = pd.to_numeric(df["uncertainty"], errors="coerce")
            else:
                df["uncertainty"] = np.nan

            pivot_true = df.pivot_table(index="_key_", columns="isotope", values="true", aggfunc="first")
            pivot_true.columns = [str(c) for c in pivot_true.columns]

            pivot_unc = df.pivot_table(index="_key_", columns="isotope", values="uncertainty", aggfunc="first")
            pivot_unc.columns = [str(c) for c in pivot_unc.columns]

            wide = base.merge(pivot_true, left_on="_key_", right_index=True, how="left")
            if not pivot_unc.empty:
                wide = wide.merge(pivot_unc.add_suffix("_uncertainty"), left_on="_key_", right_index=True, how="left")
        else:
            wide = base.copy()

        # Role flags
        role_to_col = {
            "calibration": "is_calibration_id",
            "validation":  "is_validation_id",
            "control":     "is_control_id",
            "linearity":   "is_linearity_id",
            "memory":      "is_memory_id",
            "drift":       "is_drift_id",
            "blank":       "is_blank_id",
        }
        for col in role_to_col.values():
            if col not in wide.columns:
                wide[col] = 0

        # Build roles map (from role_map or df['roles'])
        def _split_roles(val):
            if val is None: return []
            s = str(val).strip()
            if not s: return []
            return [p.strip() for p in s.replace(",", ";").split(";") if p.strip()]

        key_to_roles = {}
        if isinstance(role_map, dict) and role_map:
            tmp = {}
            for role, ids in role_map.items():
                for sid in ids or []:
                    k = self._normalize_id(sid)
                    if k:
                        tmp.setdefault(k, set()).add(str(role).strip().lower())
            key_to_roles = tmp
        elif "roles" in df.columns:
            for k, sub in df[["_key_", "roles"]].dropna().groupby("_key_"):
                seen = set()
                for r in sub["roles"].tolist():
                    seen.update(_split_roles(r))
                if seen:
                    key_to_roles[k] = {rr.lower() for rr in seen}

        for i, row in wide.iterrows():
            k = row["_key_"]
            rset = key_to_roles.get(k, set())
            for role, colname in role_to_col.items():
                if role.lower() in rset:
                    wide.at[i, colname] = 1

        # Column order: IDs, display isotopes (true values), uncertainties, then flags
        disp_isos = [c for c in wide.columns if c not in ("_key_","sample_id","ref_name")
                    and not c.endswith("_uncertainty")
                    and not str(c).lower().startswith("is_")]
        compat_unc  = [f"{c}_uncertainty" for c in disp_isos if f"{c}_uncertainty" in wide.columns]
        role_cols   = [c for c in wide.columns if str(c).lower().startswith("is_")]

        ordered = ["sample_id","ref_name"] + sorted(disp_isos) + sorted(compat_unc) + sorted(role_cols)
        ordered = [c for c in ordered if c in wide.columns]
        out = wide[ordered].copy()

        # Numeric formatting-friendly
        for c in disp_isos + compat_unc:
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors="coerce")

        return out

    def update_standards_table(self, data, from_lims=False):

        if data is None:
            self.standards_table.setRowCount(0)
            return
        df = data.copy()
        df.columns = [str(col).strip() for col in df.columns]

        # NEW: roles & IDs first
        try:
            df = self._order_standard_columns(df)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        self.standards_table.blockSignals(True)
        self.standards_table.setRowCount(len(df))
        self.standards_table.setColumnCount(len(df.columns))
        self.standards_table.setHorizontalHeaderLabels(df.columns)

        bool_cols = [c for c in df.columns if c.lower().startswith("is_")]

        if from_lims:
            for r_idx, row in df.iterrows():
                for c_idx, col_name in enumerate(df.columns):
                    item = QTableWidgetItem()
                    if col_name.lower() in bool_cols:
                        item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                        is_checked = pd.to_numeric(row[col_name], errors='coerce') == 1
                        item.setCheckState(Qt.Checked if is_checked else Qt.Unchecked)
                        item.setText("")  # keep cell narrow
                    else:
                        item.setText(str(row[col_name]))
                    self.standards_table.setItem(r_idx, c_idx, item)
        else:
            for r_idx, row in df.iterrows():
                for c_idx, col_name in enumerate(df.columns):
                    item = QTableWidgetItem()
                    if col_name.lower() in bool_cols:
                        item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                        chk = pd.to_numeric(row[col_name], errors='coerce')
                        item.setCheckState(Qt.Checked if chk in [1, -1] else Qt.Unchecked)
                        item.setText("")
                    else:
                        item.setText(str(row[col_name]))
                    self.standards_table.setItem(r_idx, c_idx, item)

        # Make roles compact & keep table readable in small windows
        try:
            header = self.standards_table.horizontalHeader()
            # Default: interactive (user can resize), last section stretches
            header.setStretchLastSection(True)
            header.setSectionResizeMode(QHeaderView.Interactive)

            # Keep ID and role columns tight so they stay visible
            tight_first = [i for i, c in enumerate(df.columns)
                        if c in ("sample_id","ref_name","name","id") or c.lower().startswith("is_") or c.lower() in ("role_code","role","rolecode")]
            for idx in tight_first:
                header.setSectionResizeMode(idx, QHeaderView.ResizeToContents)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try:
            self._freeze_sample_id(self.standards_table, df)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        self.standards_table.resizeColumnsToContents()
        self.standards_table.blockSignals(False)
    
    def _log_validation_summary(self, validation_results):
        """
        Log validation for control/QC samples:
        1) Prefer validation_results if it contains residuals/z.
        2) Otherwise synthesize from analysis_data + standards_data for control IDs (from roles or role_code).
        """
    
        try:
            def _fmt(x, places=3):
                try:
                    return f"{float(x):.{places}g}"
                except Exception as e:

                    logging.warning(f"Exception caught: {e}"); return "NA"
            # --- Helper: find control IDs from roles or standards ---
            def _control_ids():
                ids = set()
                # from worker roles (preferred)
                roles = getattr(self, "worker", None)
                roles = getattr(roles, "roles", None) if roles is not None else None
                # or from window (if you keep it there)
                if not roles:
                    roles = getattr(self, "roles", None)

                control_keys = {"CTRL","SCTRL","CONTROL","QC","QCTRL","VALIDATION","VAL"}
                if isinstance(roles, dict):
                    for k, v in roles.items():
                        try:
                            key = str(k).upper().strip()
                        except Exception as e:

                            logging.warning(f"Exception caught: {e}"); continue
                        if key in control_keys:
                            if isinstance(v, (list, tuple, set)):
                                ids.update(str(x) for x in v)
                            elif v is not None:
                                ids.add(str(v))
                # from standards_data role_code if we still have none
                if not ids and isinstance(getattr(self, "standards_data", None), pd.DataFrame):
                    sdf = self.standards_data
                    if "role_code" in sdf.columns and "sample_id" in sdf.columns:
                        mask = sdf["role_code"].astype(str).str.upper().isin(control_keys)
                        ids.update(sdf.loc[mask, "sample_id"].astype(str))
                return ids

            # --- Case A: validation_results provided and useful ---
            vr = validation_results
            if isinstance(vr, pd.DataFrame) and not vr.empty:
                df = vr.copy()
                # filter to control rows if role_code present
                if "role_code" in df.columns:
                    mask = df["role_code"].astype(str).str.upper().isin(["CTRL","SCTRL","CONTROL","QC","QCTRL","VALIDATION","VAL"])
                    if mask.any():
                        df = df.locmask.copy()

                has_iso = "isotope" in df.columns
                has_res = "residual" in df.columns
                has_z   = "z" in df.columns

                # If it already has isotope + residual, log directly
                if has_iso and (has_res or has_z):
                    lines = []
                    sid_series = df["sample_id"] if "sample_id" in df.columns else pd.Series(["(unknown)"] * len(df))
                    for sid, sub in df.groupby(sid_series):
                        label = str(sid)
                        if "ref_name" in sub.columns:
                            uniq = sub["ref_name"].astype(str).dropna().unique()
                            if len(uniq) == 1 and uniq[0]:
                                label = f"{label} ({uniq[0]})"
                        parts = []
                        for _, row in sub.iterrows():
                            iso = row.get("isotope", "")
                            res = row.get("residual", None)
                            z   = row.get("z", None)
                            if pd.notnull(z):
                                tag = "PASS" if abs(float(z)) <= 2 else "FAIL"
                                parts.append(f"{iso}: resid={_fmt(res)}‰, z={float(z):.2f} {tag}")
                            elif pd.notnull(res):
                                parts.append(f"{iso}: resid={_fmt(res)}‰")
                        if parts:
                            lines.append(f"{label}: " + " | ".join(parts))
                    if lines:
                        logging.info("Validation summary: " + " || ".join(lines))
                        return
                    # fall through if no parts collected

            # --- Case B: synthesize from analysis_data + standards_data for control IDs ---
            a = getattr(self, "analysis_data", None)
            s = getattr(self, "standards_data", None)
            if not (isinstance(a, pd.DataFrame) and not a.empty and isinstance(s, pd.DataFrame) and not s.empty):
                logging.info("Validation summary: none (no usable analysis/standards)")
                return

            ctrl_ids = _control_ids()
            if not ctrl_ids:
                logging.info("Validation summary: none (no control IDs)")
                return

            a[2] = a.copy()
            s[2] = s.copy()
            # Ensure string ids
            if "sample_id" in a[2].columns:
                a[2]["sample_id"] = a[2]["sample_id"].astype(str).str.strip()
            if "sample_id" in s[2].columns:
                s[2]["sample_id"] = s[2]["sample_id"].astype(str).str.strip()

            a[2] = a[2][["sample_id"].isin(ctrl_ids)] if "sample_id" in a[2].columns else a[2]

            # Build tidy residuals for isotopes we can match
            iso_candidates = ("d18O","dD","d17O","d13C","d15N")
            rows = []
            for iso in iso_candidates:
                cal = f"{iso}_calibrated"
                tru_candidates = [f"{iso}_true", f"{iso.lower()}_true", iso]  # last fallback: plain iso in standards
                if cal not in a[2].columns:
                    continue

                # find matching true column
                true_col = None
                for tc in tru_candidates:
                    if tc in s[2].columns:
                        true_col = tc
                        break
                if true_col is None:
                    continue

                m = a[2][["sample_id", cal]]
                j = m.merge(s[2][["sample_id", true_col, *(["ref_name"] if "ref_name" in s[2].columns else [])]],
                            on="sample_id", how="left")
                j["residual"] = pd.to_numeric(j[cal], errors="coerce") - pd.to_numeric(j[true_col], errors="coerce")
                j["isotope"] = iso
                rows.append(j[["sample_id","ref_name","isotope","residual"] if "ref_name" in j.columns else ["sample_id","isotope","residual"]])

            if not rows:
                logging.info("Validation summary: none (no matched calibrated/true pairs)")
                return

            tidy = pd.concat(rows, ignore_index=True)
            lines = []
            sid_series = tidy["sample_id"] if "sample_id" in tidy.columns else pd.Series(["(unknown)"] * len(tidy))
            for sid, sub in tidy.groupby(sid_series):
                label = str(sid)
                if "ref_name" in sub.columns:
                    uniq = sub["ref_name"].astype(str).dropna().unique()
                    if len(uniq) == 1 and uniq[0]:
                        label = f"{label} ({uniq[0]})"
                parts = [f"{row['isotope']}: resid={_fmt(row['residual'])}‰" for _, row in sub.iterrows()]
                if parts:
                    lines.append(f"{label}: " + " | ".join(parts))

            if lines:
                logging.info("Validation summary: " + " || ".join(lines))
            else:
                logging.info("Validation summary: none")

        except Exception as e:
            logging.debug(f"Validation logging skipped: {e}")

    def _inject_validation_zscores(self, analysis_df, standards_df, roles):
        """
        Return a copy of analysis_df with per-isotope z-score columns added for the
        selected validation IDs (first one if multiple). Cells are filled only for rows
        whose sample_id matches the chosen validation ID(s); others are NaN.
        Columns created: '<iso>_z' (e.g., d18O_z, dD_z).
        """

        try:
            if analysis_df is None or analysis_df.empty:
                return analysis_df

            # Which validation IDs?
            val_ids = []
            r = roles or {}
            v = r.get("validation")
            if isinstance(v, str) and v.strip():
                val_ids = [v.strip()]
            elif isinstance(v, (list, tuple, set)):
                val_ids = [str(x).strip() for x in v if str(x).strip()]

            if not val_ids:
                return analysis_df

            # True values must be present in standards_df in WIDE form ({iso}_true / {iso}_uncertainty)
            if not isinstance(standards_df, pd.DataFrame) or standards_df.empty or "sample_id" not in standards_df.columns:
                return analysis_df

            sdf = standards_df.copy()
            sdf["sample_id"] = sdf["sample_id"].astype(str).str.strip()

            # Figure out which isotopes are present in analysis_df (calibrated)
            iso_list = [iso for iso in ("d18O","dD","d17O","d13C","d15N")
                        if any(str(c).startswith(f"{iso}_calibrated") for c in analysis_df.columns)]

            out = analysis_df.copy()
            out["sample_id"] = out["sample_id"].astype(str).str.strip()

            chosen = val_ids  # support multiple; fill for all

            for iso in iso_list:
                cal_col = f"{iso}_calibrated"
                if cal_col not in out.columns:
                    continue

                true_col = f"{iso}_true"
                unc_col  = f"{iso}_uncertainty"
                if true_col not in sdf.columns:
                    # If your standards are in LONG format, stop here (we keep it simple/minimal)
                    continue

                # Map true/unc for chosen validation IDs
                true_map = sdf.set_index("sample_id")[true_col].to_dict()
                unc_map  = sdf.set_index("sample_id")[unc_col].to_dict() if unc_col in sdf.columns else {}

                zcol = f"{iso}_z"
                # init as NaN
                outzcol = np.nan

                # compute z only for rows whose sample_id is in chosen validation IDs
                mask = out["sample_id"].isin(chosen)
                if not mask.any():
                    continue

                # z = (measured - true) / sigma ; fall back sigma to 1.0 if missing to avoid div/0
                true_vals = out.loc[mask, "sample_id"].map(true_map)
                sigmas    = out.loc[mask, "sample_id"].map(unc_map).astype(float)
                # safe sigma
                sigmas = sigmas.where(sigmas > 0, other=1.0)

                z = (pd.to_numeric(out.loc[mask, cal_col], errors="coerce") - pd.to_numeric(true_vals, errors="coerce")) / sigmas
                out.loc[mask, zcol] = z.values

            return out
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return analysis_df

    def _memory_model_needs_analysis(self) -> bool:
        """Return True only for per-analysis/intra-sample memory models."""
        try:
            txt = (self.memory_combo.currentText() or "").strip().lower()
        except Exception:
            txt = ""
        return any(k in txt for k in ("intra", "per analysis", "Exponential", "Asymptotic"))
        
    def on_plot_selection_changed(self, plot_name):
        if plot_name in ["Memory Fit", "Combined Fit Diagnostics"]:
            if self.isotope_combo.currentText() and self.memory_fits and self.isotope_combo.currentText() in self.memory_fits:
                 self.analysis_combo.clear(); self.analysis_combo.addItems([str(k) for k in self.memory_fits[self.isotope_combo.currentText()].keys()])
            self.analysis_combo_label.show(); self.analysis_combo.show()
            self.dynamic_controls_widget.show()
        elif plot_name == "Drift Fit":
            self.analysis_combo_label.hide(); self.analysis_combo.hide()
            self.dynamic_controls_widget.hide()

    def _get_sample_ids_for_plot(self, role_col="is_drift_id", isotope=None):
        """
        Return a sorted list of sample IDs flagged for a given role (e.g., drift, memory) for the current isotope.
        """
        std = getattr(self, "standards_data", None)
        if std is None or std.empty or "sample_id" not in std.columns or role_col not in std.columns:
            return []
        # If standards are per-isotope, filter by isotope
        if "isotope" in std.columns and isotope:
            mask = (std["isotope"].astype(str) == str(isotope))
            std = std.loc[mask]
        ids = std.loc[std[role_col] == 1, "sample_id"].astype(str).unique().tolist()
        return sorted(ids)

    # --- Auto-plot helpers -------------------------------------------------
    def _schedule_auto_plot(self, delay_ms: int = 80):
        """Debounced auto-plot trigger to keep the UI snappy."""
        try:
            if getattr(self, "_auto_plot_timer", None):
                self._auto_plot_timer.stop()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        self._auto_plot_timer = QTimer(self)
        self._auto_plot_timer.setSingleShot(True)
        self._auto_plot_timer.timeout.connect(self._auto_plot_safe)
        self._auto_plot_timer.start(max(0, int(delay_ms)))

    def _auto_plot_safe(self):
        """Render the current plot silently; swallow benign issues."""
        try:
            self.plot_on_canvas()
        except Exception as e:
            # Keep this quiet—auto-plot shouldn't nag the user
            logging.debug(f"Auto-plot skipped: {e}")

    def plot_on_canvas(self):
        data_to_use, _, mpl_config, plot_name, sample_ids = self._get_plot_details()
        if data_to_use is None: return
        
        plot_func, plot_args = mpl_config['func'], mpl_config['args']
        
        try:
            self.figure.clear()
            mwl_to_plot = getattr(self, "mwl_combo", None).currentText() if hasattr(self, "mwl_combo") else "None"
            plot_func(self.figure, data_to_use, *plot_args, mwl=mwl_to_plot, sample_ids=sample_ids,
                        **self.plot_configs.get(plot_name, {}).get("kwargs", {}))
            self.canvas.draw()
        except Exception as e:
            logging.error(f"Failed to generate canvas plot '{plot_name}': {e}", exc_info=True)
            QMessageBox.critical(self, "Plot Error", f"Could not generate plot on canvas: {e}")

    def open_plot_in_browser(self):
        data_to_use, config, _, plot_name, _sample_ids = self._get_plot_details()
        if data_to_use is None: return
        plot_func, plot_args = config['func'], config['args']
        try:
            output_file = "temp_plot.html"
            mwl_to_plot = getattr(self, "mwl_combo", None).currentText() if hasattr(self, "mwl_combo") else "None"
            plot_func(data_to_use, *plot_args, output_file=output_file, mwl=mwl_to_plot,
                    **self.plot_configs.get(plot_name, {}).get("kwargs", {}))
            webbrowser.open('file://' + os.path.realpath(output_file))
        except Exception as e:
            logging.error(f"Failed to generate browser plot '{plot_name}': {e}", exc_info=True)
            QMessageBox.critical(self, "Plot Error", f"Could not generate plot in browser: {e}")

    def _ensure_laser_option_controls(self):        
        """Create Laser post-processing controls once and keep as attributes."""
        if not hasattr(self, "linearity_combo"):
            self.linearity_combo = QComboBox(self.input_panel)
            self.linearity_combo.addItems(["Linear", "None"])
        if not hasattr(self, "memory_combo"):
            self.memory_combo = QComboBox(self.input_panel)
            self.memory_combo.addItems(["1-Reservoir (Single Pool)","Fast/Slow Pool Carryover", "Exponential", "Asymptotic", "None"])
        if not hasattr(self, "drift_combo"):
            self.drift_combo = QComboBox(self.input_panel)
            self.drift_combo.addItems(["Linear", "None"])
        if not hasattr(self, "mwl_combo"):
            self.mwl_combo = QComboBox(self.input_panel)
            self.mwl_combo.addItems(["None", "GMWL_Craig", "GMWL_Gat", "GardMWL", "HerMWL", "MMWL"])
        if not hasattr(self, "outlier_method_combo"):
            self.outlier_method_combo = QComboBox(self.input_panel)
            self.outlier_method_combo.addItems(OUTLIER_METHODS)
            self.outlier_method_combo.setToolTip(
                "Outlier detection method for injections.\n"
                "None = disabled  |  SD / MAD / Huber = enabled with factor below.")      
        if not hasattr(self, "outlier_factor_edit"):
            self.outlier_factor_edit = QLineEdit("2.0", self.input_panel)

    def _first_nonempty_df(self, *names):
        
        for nm in names:
            df = getattr(self, nm, None)
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
        return None
       
    def _infer_run_times_from_data(self):
        
        """
        Infer (run_start_time, run_end_time) from the current dataset.
        Prefers injection_data → data → analysis_data.
        Looks for common datetime or date+time columns, parses and returns min/max.
        Returns (start, end) as naive datetime.datetime; raises RuntimeError if no rows.
        """

        # 1) pick the best-available dataframe
        df = None
        for nm in ("injection_data", "data", "analysis_data"):
            obj = getattr(self, nm, None)
            if isinstance(obj, pd.DataFrame) and not obj.empty:
                df = obj
                break
        if df is None or df.empty:
            raise RuntimeError("No data available to infer run times.")

        # normalize column names lookup (lower → original)
        cols = list(df.columns)
        lowmap = {str(c).lower().strip(): c for c in cols}

        # helpers
        def _parse_series(x):
            s = pd.to_datetime(x, errors="coerce", infer_datetime_format=True)
            # drop tz info if present
            try:
                s = s.dt.tz_localize(None)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            return s

        candidates = []

        # 2) direct datetime-like columns (most common)
        direct_names = (
            "timestamp","datetime","date_time","date time","acq datetime","acq date time",
            "acquisitiontime","acquisition time","analysis time","analysistime",
            "run time","runtime","time"  # some exports put everything in "Time"
        )
        for name in direct_names:
            if name in lowmap:
                s = _parse_series(df[lowmap[name]])
                if s.notna().sum() >= 2:
                    candidates.append(("direct", name, s))

        # 3) separate date + time columns
        date_names = [n for n in lowmap if n in ("date","acq date","acqdate","run date","rundate")]
        time_names = [n for n in lowmap if n in ("time","acq time","acqtime","run time","runtime")]
        if date_names and time_names:
            dcol = lowmap[date_names[0]]
            tcol = lowmap[time_names[0]]
            combo = (df[dcol].astype(str).str.strip() + " " + df[tcol].astype(str).str.strip()).replace({"nan nan": np.nan})
            s = _parse_series(combo)
            if s.notna().sum() >= 2:
                candidates.append(("combo", f"{date_names[0]} + {time_names[0]}", s))

        # 4) already-datetime typed columns
        for c in df.select_dtypes(include=["datetime64[ns]", "datetime64[ns, UTC]"]).columns:
            s = _parse_series(df[c])
            if s.notna().sum() >= 2:
                candidates.append(("dtype", str(c), s))

        # 5) choose best candidate: max non-nulls, then widest span
        if candidates:
            best = None
            best_score = (-1, dt.timedelta(0))
            for kind, name, s in candidates:
                n = int(s.notna().sum())
                span = (s.max() - s.min()) if n >= 2 else dt.timedelta(0)
                key = (n, span)
                if key > best_score:
                    best_score = key
                    best = s
            s = best.dropna()
            start, end = s.min().to_pydatetime(), s.max().to_pydatetime()
            logging.info(f"Inferred run times from '{name}': start={start}, end={end} (n={len(s)})")
            return start, end

        # 6) fallback: fabricate from row order (1 second apart)
        now = dt.datetime.now()
        n = len(df)
        start = now
        end = now + dt.timedelta(seconds=max(0, n - 1))
        logging.warning("No parseable timestamp found; using row-order fallback for run times.")
        return start, end

    def _update_save_to_db_enabled(self):
        
        """Enable 'Save to Database' when DSN + RunID + some data present.
        Also ensure parent group is enabled and refresh styles.
        """

        # Gather state
        dsn_ok = bool(getattr(self, "dsn_edit", None) and self.dsn_edit.text().strip())
        run_ok = bool(getattr(self, "run_id_edit", None) and self.run_id_edit.text().strip())

        has_df = False
        for nm in ("analysis_data", "injection_data", "data"):
            df = getattr(self, nm, None)
            if isinstance(df, pd.DataFrame) and not df.empty:
                has_df = True
                break

        ok = dsn_ok and run_ok and has_df and self.has_admin_priv

        # There might be more than one button instance if the panel was rebuilt.
        buttons = self.findChildren(QPushButton, "saveDbBtn")
        if not buttons and hasattr(self, "save_db_btn"):
            buttons = [self.save_db_btn]

        # Ensure LIMS block is enabled (parent disabled → child looks disabled)
        try:
            if hasattr(self, "lims_based_widget") and self.lims_based_widget:
                self.lims_based_widget.setEnabled(True)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # Apply enable + restyle on all matching buttons
        for btn in buttons:
            try:
                btn.setEnabled(bool(ok))
                # force stylesheet refresh (Qt sometimes caches state visuals)
                st = btn.style()
                if st is not None:
                    st.unpolish(btn)
                    st.polish(btn)
                btn.update()
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

        logging.info(
            "SaveDB enabled? dsn=%s run=%s data=%s (si_run_id=%s) | buttons=%s",
            dsn_ok, run_ok, has_df, getattr(self, 'si_analysis_run_id', None),
            [(b.isVisible(), b.isEnabled()) for b in buttons]
        )

    def _save_results_to_db_clicked(self):
        
        # --- DSN guard ---
        dsn = getattr(self, "dsn_edit", None).text().strip() if hasattr(self, "dsn_edit") and self.dsn_edit else ""
        if not dsn:
            QMessageBox.warning(self, "LIMS", "Please select a DSN file.")
            return

        # --- Resolve SIAnalysisRunID ---
        run_pk = getattr(self, "si_analysis_run_id", None)
        if run_pk is None:
            loadlist = getattr(self, "lims_loadlist", None)
            if isinstance(loadlist, pd.DataFrame) and not loadlist.empty:
                _col_map = {c.lower(): c for c in loadlist.columns}
                for key in ("sianalysisrunid", "analysisrunid", "runid"):
                    if key in _col_map:
                        ser = pd.to_numeric(loadlist[key], errors="coerce").dropna()
                        if not ser.empty:
                            run_pk = int(ser.iloc[0])
                            break
        if run_pk is None:
            QMessageBox.warning(self, "LIMS", "Could not determine SIAnalysisRunID from LIMS.")
            return

        # --- Build measurables (LIST) ---
        df_for_meas = None
        for nm in ("analysis_data", "injection_data", "data"):
            obj = getattr(self, nm, None)
            if isinstance(obj, pd.DataFrame) and not obj.empty:
                df_for_meas = obj
                break
        if df_for_meas is None:
            df_for_meas = pd.DataFrame(columns=[])

        cols_lower = [str(c).lower() for c in df_for_meas.columns]
        meas_list = [iso for iso in ("d18O", "dD", "d17O", "d13C", "d15N")
                    if any(col.startswith(iso.lower()) for col in cols_lower)]

        # --- Infer run start/end ---
        try:
            start_time, end_time = self._infer_run_times_from_data()
            self._run_start_time, self._run_end_time = start_time, end_time
        except Exception as _e:
            logging.warning(f"Could not infer run times from data ({_e}); using now().")
            start_time = getattr(self, "_run_start_time", None) or dt.datetime.now()
            end_time   = getattr(self, "_run_end_time", None)   or dt.datetime.now()

        # --- Collect payloads ---
        data_path      = getattr(self, "data_file", None) or f"LIMS:{getattr(self, '_get_run_id_text', lambda: '')()}"
        status_code    = 8
        operator_login = getpass.getuser()

        inj_df     = self._first_nonempty_df("injection_data", "data")
        results_df = self._first_nonempty_df("analysis_data")
        
        memory_fits = getattr(self, "memory_fits", None)
        drift_fits  = getattr(self, "drift_fits", None)
        mem_factors = getattr(self, "memory_factors", None)

        try:
            roles_from_gui = self.get_roles_from_table()
        except Exception:
            roles_from_gui = {}

        try:
            methods = {
                "linearity": self._get_combo_text("linearity_combo", "Off"),
                "memory":    self._get_combo_text("memory_combo",    "Two-Pool"),
                "drift":     self._get_combo_text("drift_combo",     "Linear"),
                "mwl":       self._get_combo_text("mwl_combo",       "GMWL_Craig"),
                "memory_analysis_id": getattr(self, "memory_analysis_id", 0),
            }
        except Exception:
            methods = {"memory_analysis_id": getattr(self, "memory_analysis_id", 0)}
        try:
            # For PostgreSQL the engine is already initialised by the launcher;
            # only re-initialise for Access / SQL Server DSN files.
            if db_manager.dialect != "POSTGRESQL":
                dialect = "ACCESS" if dsn.lower().endswith(('.mdb', '.accdb')) else "SQL_SERVER"
                try:
                    db_manager.get_engine()
                except Exception:
                    db_manager.initialize(dialect, dsn)

            summary = database.write_results_to_db(
                dsn_path=dsn,
                run_id=int(run_pk),
                operator_login=operator_login,
                data_path=data_path,
                measurables=meas_list,
                status_code=status_code,
                run_start=start_time,
                run_end=end_time,
                roles=roles_from_gui,
                methods=methods,
                inj_df=inj_df,
                results_df=results_df,
                standards_df=getattr(self, "standards_data", None),
                calibration_fits=self.calibration_fits,
                memory_fits=memory_fits,
                drift_fits=drift_fits,
                memory_factors=mem_factors,
            )
            logging.info("Saved to DB: %s", summary if summary is not None else "OK")

            # Persist outlier method selection (not handled by write_results_to_db)
            outlier_method = self._get_combo_text("outlier_method_combo", "")
            if outlier_method:
                try:
                    with db_manager.get_connection() as _conn:
                        _conn.execute(
                            text("UPDATE SIAM.SIAnalysisRun SET OutlierMethod = :m "
                                 "WHERE SIAnalysisRunID = :rid"),
                            {'m': outlier_method, 'rid': int(run_pk)}
                        )
                        _conn.commit()
                except Exception as _oe:
                    logging.warning(f"Could not save outlier method: {_oe}")

            QMessageBox.information(self, "LIMS", "Run saved to database successfully.")
        except Exception as e:
            logging.error("Save to DB failed", exc_info=True)
            QMessageBox.critical(self, "LIMS", f"Failed to save run to database:\n{e}")

    def _lims_update_run_metadata(self, si_analysis_run_id: int):
        try:
            
            # Build measurables string
            present = []
            df = getattr(self, "data", None) or getattr(self, "injection_data", None)
            if df is not None:
                cols = [str(c).lower() for c in df.columns]
                for iso in ("d18O","dD","d17O","d13C","d15N"):
                    if any(c.startswith(iso.lower()) for c in cols):
                        present.append(iso)
            measurables = "; ".join(present)

            start_time = getattr(self, "_run_start_time", None) or dt.datetime.now()
            end_time   = getattr(self, "_run_end_time", None)   or dt.datetime.now()

            data_path  = self.data_file or f"LIMS:{self._get_run_id_text()}"
            run_status = 8
            technician2 = getattr(self, "lims_user_id", None)
            mod_user = getpass.getuser()

            # --- CALL: No dsn ---
            database.update_si_analysis_run(
                si_analysis_run_id, # No DSN arg
                run_start_time=start_time,
                run_end_time=end_time,
                data_path=data_path,
                measurables=measurables,
                run_status=run_status,
                technician2=technician2,
                mod_user=mod_user,
            )
        except Exception as e:
            logging.error("LIMS: SIAnalysisRun update failed: %s", e, exc_info=True)

    def generate_and_open_reports(self):
        if self.analysis_data is None:
            QMessageBox.warning(self, "No Data", "No processed data to generate reports.")
            return
        try:
            output_dir = os.path.realpath("reports")
            mwl_to_plot = self.mwl_combo.currentText() if hasattr(self, "mwl_combo") and self.mwl_combo else "None"
            generate_reports(self.analysis_data,
                            self.injection_data,
                            self.validation_results,
                            output_dir,
                            mwl=mwl_to_plot,
                            memory_fits=getattr(self, "memory_fits", None),
                            drift_fits=getattr(self, "drift_fits", None),
                            standards_df=getattr(self, "standards_data", None))
            webbrowser.open(f'file://{output_dir}')
            QMessageBox.information(self, "Reports Generated", f"Reports saved in '{output_dir}'.")
        except Exception as e:
            logging.error(f"Error generating reports: {e}", exc_info=True)
            QMessageBox.critical(self, "Report Error", f"Could not generate reports: {e}")


def pair_isotopes_by_sample(df, x_col, y_col):
    
    """
    Build a paired dataset for scatter plots when isotopes are on separate rows/sheets (e.g., IRMS DI).
    Strategy:
      1) If rows already have both x_col & y_col -> use those rows.
      2) If long layout (has 'isotope' + 'delta') -> pivot to wide by sample_id.
      3) Else average per sample_id for each isotope and inner-join.
    Returns DataFrame with at least [x_col, y_col] (and sample_id if available).
    """
    if df is None or getattr(df, "empty", True):
        return df
    
    # Fast path: paired rows already present
    if x_col in df.columns and y_col in df.columns:
        paired = df[[c for c in df.columns if c in ("sample_id", x_col, y_col)]].dropna(subset=[x_col, y_col])
        if not paired.empty:
            return paired

    cols = set(map(str, df.columns))

    # Long layout: isotope + delta
    if {"isotope", "delta"}.issubset(cols) and "sample_id" in cols:
        tmp = df.copy()
        tmp["iso_norm"] = tmp["isotope"].astype(str).str.strip().str.replace("δ", "d")
        wide = tmp.pivot_table(index="sample_id", columns="iso_norm", values="delta", aggfunc="mean").reset_index()
        if x_col in wide.columns and y_col in wide.columns:
            return wide.dropna(subset=[x_col, y_col])

    # Wide but unpaired: mean per sample_id + join
    if "sample_id" in df.columns:
        parts = []
        for col in (x_col, y_col):
            if col in df.columns:
                parts.append(df[["sample_id", col]].dropna().groupby("sample_id", as_index=False)[col].mean())
        if len(parts) == 2 and not parts[0].empty and not parts[1].empty:
            joined = parts[0].merge(parts[1], on="sample_id", how="inner")
            if not joined.empty:
                return joined

    # If nothing worked, return empty scaffold
    cols_out = ["sample_id"] if "sample_id" in df.columns else []
    cols_out += [x_col, y_col]
    return pd.DataFrame(columns=cols_out)

def _iso_norm(s: str) -> str:
    
    """Normalize isotope tags so filters match (handles 'δ'→'d')."""
    return str(s or "").replace("δ", "d").strip()

def _canon_lookup(colnames, target):
    
    """Case/space/underscore-insensitive lookup. Returns actual column name or None."""
    norm = lambda s: re.sub(r"[\s_]+", "", str(s or "")).lower()
    wanted = norm(target)
    for c in colnames:
        if norm(c) == wanted:
            return c
    return None
    
class StandaloneProcessorWindow(QMainWindow):
    """
    A simple QMainWindow shell to host the ProcessorWidget
    when running in standalone mode.
    """
    def __init__(self, run_id=None, dsn_path=None):
        super().__init__()
        
        # Set window properties here
        self.setWindowTitle("Isotope Data Processor")
        self.resize(1600, 900)
        
        # Create and set the ProcessorWidget as the central widget
        self.processor_widget = ProcessorWidget(run_id=run_id, dsn_path=dsn_path)
        self.setCentralWidget(self.processor_widget)
        
    def closeEvent(self, event):
        # Pass the close event to the processor widget for settings-saving
        self.processor_widget.closeEvent(event)
        super().closeEvent(event)

        
if __name__ == '__main__':
    
    # Enable high-DPI scaling
    os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    if hasattr(Qt, 'AA_DontUseNativeDialogs'):
        QApplication.setAttribute(Qt.AA_DontUseNativeDialogs, True)
        
    app = QApplication(sys.argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
                        handlers=[logging.FileHandler('isotope_processing.log', mode='w'), logging.StreamHandler()])

    # Set larger default font for 4K
    font = QFont()
    import platform
    font.setPointSize(11 if platform.system() == "Darwin" else 9)
    app.setFont(font)
    
    # Capture command-line arguments ---
    run_id = None
    dsn_path = None
    # sys.argv: [script, run_id, dsn_path, dialect]
    if len(sys.argv) >= 3:
        run_id = sys.argv[1]
        dsn_path = sys.argv[2]
        dialect = sys.argv[3] if len(sys.argv) > 3 else "SQL_SERVER"
        logging.info(f"Received arguments: Run ID={run_id}, Dialect={dialect}")
        # Initialize db_manager in this subprocess
        try:
            db_manager.initialize(dialect, dsn_path)
        except Exception as e:
            logging.error(f"DB init failed: {e}")
    elif len(sys.argv) == 2:
        logging.warning(f"Received only one argument: {sys.argv[1]}. Expected Run ID and DSN Path.")
    else:
        logging.info("No arguments received, launching in standalone mode.")

    # Pass captured arguments to the MainWindow constructor
    window = StandaloneProcessorWindow(run_id=run_id, dsn_path=dsn_path)
    window.show()
    sys.exit(app.exec_())
    
