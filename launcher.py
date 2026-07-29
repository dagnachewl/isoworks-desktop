"""
launcher.py — Main application entry point and window manager for IsoWorks.
Builds the QMainWindow shell with the animated sidebar, lazy-loads all
module panels into a QStackedWidget, and bootstraps the database connection.
"""
from __future__ import annotations
import sys
import logging
import os
import glob
from typing import Optional
from functools import partial 

from PyQt5.QtCore import (
    Qt, QSettings, QSize
)
from PyQt5.QtGui import QIcon, QFont, QColor, QKeySequence
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QStackedWidget, QHBoxLayout, 
    QVBoxLayout, QLabel, QPushButton, QAction, QToolBar, QMessageBox,
    QToolButton, QTextEdit, QDialog, QSizePolicy, QShortcut
)

from db_core import db_manager
from shared_utils import IconCache, ModuleSpec, ComingSoonWidget
from help_browser import show_help

# --- Lazy Loader Factories ---
def load_settings_widget():
    from settings_module import SettingsWidget
    return SettingsWidget()

def load_project_list():
    from project_list_gui import ProjectListWindow
    return ProjectListWindow()

def load_sample_submission():
    from sample_submission_gui import SampleSubmissionWindow
    return SampleSubmissionWindow()

def load_samples_tba_management():
    from samples_tba_management_gui import SamplesTBAWindow
    return SamplesTBAWindow()

def load_workflow_assign():
    from sample_workflow_assign_gui import SiamWorkflowAssignWindow
    return SiamWorkflowAssignWindow()

def load_siam_run_list():
    from siam_run_list_gui import SiamRunListWindow
    return SiamRunListWindow()

def load_siam_preanalysis():
    try:
        from siam_preanalysis_runs_gui import PreAnalysisRunListWindow
        return PreAnalysisRunListWindow()
    except ImportError:
        return ComingSoonWidget("Pre-Analysis Batches", "Module not found or failed to load.")

def load_dashboard():
    try:
        from dashboard_gui import DashboardWidget
        return DashboardWidget()
    except ImportError:
        return ComingSoonWidget("Dashboard", "Module not found or failed to load.")

def load_processor_widget():
    try:
        from siam_processor.views.main_window import ProcessorWidget
        return ProcessorWidget()
    except ImportError:
        return ComingSoonWidget("Processor", "Module not found or failed to load.")

def load_trims_distillation():
    try:
        from trims_distillation_runs_gui import TrimsDistillationRunsWindow
        return TrimsDistillationRunsWindow()
    except ImportError:
        return ComingSoonWidget("TRIMS Distillation", "Module not found.")

def load_trims_electrolysis():
    try:
        from trims_electrolysis_runs_gui import TrimsElectrolysisRunsWindow
        return TrimsElectrolysisRunsWindow()
    except ImportError:
        return ComingSoonWidget("TRIMS Electrolysis Enrichment", "Module not found.")

def load_trims_lsc_runs():
    try:
        from trims_lsc_runs_gui import TrimsLSCRunsWindow
        return TrimsLSCRunsWindow()
    except ImportError:
        return ComingSoonWidget("TRIMS LSC Runs", "Module not found.")

def load_trims_chem_enrichment():
    try:
        from trims_chemical_enrichment_runs_gui import ChemEnrRunsWindow
        return ChemEnrRunsWindow()
    except ImportError:
        return ComingSoonWidget("TRIMS Chemical Enrichment", "Module not found.")

# --- NGAM Module Loaders ---
def load_ngam_ingrowth_runs():
    try:
        from ngam_ingrowth_runs_gui import NGAMIngrowthRunsWindow
        return NGAMIngrowthRunsWindow()
    except ImportError:
        return ComingSoonWidget("3He Ingrowth Runs", "Module not found.")

def load_ngam_extraction_runs():
    try:
        from ngam_extraction_runs_gui import NGAMExtractionRunsWindow
        return NGAMExtractionRunsWindow()
    except ImportError:
        return ComingSoonWidget("NG Extraction Runs", "Module not found.")

def load_ngam_ng_sequence_runs():
    try:
        from ngam_ng_sequence_runs_gui import NGAMNGSequenceRunsWindow
        return NGAMNGSequenceRunsWindow()
    except ImportError:
        return ComingSoonWidget("NG MS Sequence Runs", "Module not found.")

def load_ngam_sequence_runs():
    try:
        from ngam_sequence_runs_gui import NGAMSequenceRunsWindow
        return NGAMSequenceRunsWindow()
    except ImportError:
        return ComingSoonWidget("3He Measurement Runs", "Module not found.")

def load_ngam_import_from_protocol():
    try:
        from ngam_import_from_protocol_gui import ImportFromProtocolWidget
        return ImportFromProtocolWidget()
    except ImportError:
        return ComingSoonWidget("Import from Protocol", "Module not found.")

def load_ngam_eqw_cf():
    try:
        from ngam_eqw_cf_gui import EqwCFWidget
        return EqwCFWidget()
    except ImportError:
        return ComingSoonWidget("EQW Correction Factors", "Module not found.")

# --- AMS Module Loaders ---
def load_ams_run_list():
    try:
        from ams_run_list_gui import AMSRunListWindow
        return AMSRunListWindow()
    except ImportError:
        return ComingSoonWidget("AMS Runs", "Module not found.")

def load_graphitisation():
    try:
        from ams_graphitisation_run_list_gui import GraphitisationRunListWindow
        return GraphitisationRunListWindow()
    except ImportError:
        return ComingSoonWidget("Graphitisation", "Module not found.")

def load_physical_prep():
    try:
        from ams_physical_prep_run_list_gui import PhysicalPrepRunListWindow
        return PhysicalPrepRunListWindow()
    except ImportError:
        return ComingSoonWidget("Physical Prep", "Module not found.")

def load_pretreatment():
    try:
        from ams_pretreatment_run_list_gui import PretreatmentRunListWindow
        return PretreatmentRunListWindow()
    except ImportError:
        return ComingSoonWidget("Pretreatment", "Module not found.")

def load_purification():
    try:
        from ams_purification_run_list_gui import PurificationRunListWindow
        return PurificationRunListWindow()
    except ImportError:
        return ComingSoonWidget("Purification", "Module not found.")

# --- Management Module Loaders ---
def load_employee_mgmt():
    from employee_management_gui import EmployeeModuleWidget
    return EmployeeModuleWidget()

def load_customer_mgmt():
    from customer_management_gui import CustomerManagementWidget
    return CustomerManagementWidget()

def load_equipment_mgmt():
    from equipment_management_gui import EquipmentManagementWidget
    return EquipmentManagementWidget()

def load_procedure_mgmt():
    from procedure_management_gui import ProcedureManagementWidget
    return ProcedureManagementWidget()

def load_reference_mgmt():
    from reference_management_gui import ReferenceManagementWidget
    return ReferenceManagementWidget()

def load_workflow_mgmt():
    from workflow_management_gui import WorkflowManagementWidget
    return WorkflowManagementWidget()

def load_global_params():
    from global_parameters_gui import GlobalParametersWidget
    return GlobalParametersWidget()

def load_cims():
    from cims_gui import CIMSWidget
    return CIMSWidget()

def load_qaqc():
    try:
        from qaqc_gui import QaQcWindow
        return QaQcWindow()
    except ImportError:
        return ComingSoonWidget("QA/QC", "Module not found.")

def load_audit_viewer():
    from audit_viewer_gui import AuditViewerWidget
    return AuditViewerWidget()

ICON_COLORS = {
    "dashboard": QColor("#3B82F6"),    # Blue
    "sample":    QColor("#10B981"),    # Emerald/green
    "siam":      QColor("#06B6D4"),    # Cyan/teal
    "trims":     QColor("#F59E0B"),    # Amber
    "ngam":      QColor("#A855F7"),    # Purple – Noble Gas
    "qaqc":      QColor("#8B5CF6"),    # Violet
    "ams":       QColor("#EF4444"),    # Red – AMS 14C
    "settings":  QColor("#9CA3AF"),    # Cool gray

    "list":      QColor("#9CA3AF"),
    "action":    QColor("#60A5FA"),
}

class AppShell(QMainWindow):
    ORG = "YourOrg"
    APP = "ISOTOPES_SUITE"
    
    ICON_BAR_WIDTH = 60
    SUB_MODULE_PANEL_WIDTH = 240
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Isotope Analysis Suite")
        self.setMinimumSize(1100, 700)
        
        # Initialize Settings
        self.settings = QSettings(self.ORG, self.APP)
        
        # Initialize DB Engine
        self._load_connection_from_settings()
        
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QHBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        
        # --- Build the Sidebar ---
        self.sidebar_widget = QWidget()
        self.sidebar_layout = QHBoxLayout(self.sidebar_widget)
        self.sidebar_layout.setContentsMargins(0, 0, 0, 0)
        self.sidebar_layout.setSpacing(0)
        
        # 1. The Permanent Icon Bar
        self.permanent_icon_bar = QWidget()
        self.permanent_icon_bar.setObjectName("PermanentIconBar")
        self.permanent_icon_bar.setFixedWidth(self.ICON_BAR_WIDTH)
        self.permanent_icon_bar_layout = QVBoxLayout(self.permanent_icon_bar)
        self.permanent_icon_bar_layout.setContentsMargins(5, 5, 5, 10)
        self.permanent_icon_bar_layout.setSpacing(5)
        
        # 2. The Sub-Module "Input Panel"
        self.sub_module_panel = QWidget()
        self.sub_module_panel.setObjectName("SubModulePanel")
        self.sub_module_panel.setFixedWidth(self.SUB_MODULE_PANEL_WIDTH)
        self.sub_module_panel_layout = QVBoxLayout(self.sub_module_panel)
        self.sub_module_panel_layout.setContentsMargins(10, 10, 10, 10)
        self.sub_module_panel_layout.setSpacing(10)
        self.sub_module_panel.setVisible(False)
        
        self.sidebar_layout.addWidget(self.permanent_icon_bar)
        self.sidebar_layout.addWidget(self.sub_module_panel)
        self.main_layout.addWidget(self.sidebar_widget)

        self.stack = QStackedWidget()
        self.stack.setObjectName("ModuleStack")
        self.main_layout.addWidget(self.stack, 1)
        
        self._build_menu()
        self._build_toolbar()
        
        self.modules: list[ModuleSpec] = []
        self.settings_spec: Optional[ModuleSpec] = None
        self._build_module_registry()
        
        # Maps to track loaded state
        self.key_to_widget_map = {} 
        self.key_to_spec_map = {}  # maps module keys to Specs for lazy loading
        self.icon_button_map = {}
        self.current_top_module_key: Optional[str] = None
        
        self._inflate_modules_metadata()
        self._build_sub_module_panel_widgets()
        
        self._restore_ui_state()
        self._apply_styles()

        # --- Global F1 Help Shortcut ---
        self.help_shortcut = QShortcut(QKeySequence("F1"), self)
        self.help_shortcut.setContext(Qt.ApplicationShortcut)
        self.help_shortcut.activated.connect(self._handle_global_help)

    def _apply_styles(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #F3F4F6; }
            #PermanentIconBar { background-color: #0B1220; }
            #PermanentIconBar QToolButton {
                border: none; border-radius: 4px; background: transparent; padding: 8px;
            }
            #PermanentIconBar QToolButton:hover { background-color: #1F2937; }
            #PermanentIconBar QToolButton:checked { background-color: #3B82F6; }
            #SubModulePanel { background-color: #111827; border-right: 1px solid #1F2937; }
            #SubModulePanelTitle {
                font-size: 16px; font-weight: bold; color: #E5E7EB;
                padding-bottom: 10px; border-bottom: 1px solid #374151;
            }
            #SubModulePanel QPushButton {
                background: transparent; color: #D1D5DB; border: 1px solid transparent;
                padding: 10px 8px; border-radius: 6px; text-align: left; font-size: 14px;
            }
            #SubModulePanel QPushButton:hover { background-color: #1F2937; color: #FFFFFF; }
            #SubModulePanel QPushButton[activeModule="true"] { background-color: #1F2937; color: #FFFFFF; }

            /* Fix combo-box dropdown: ensure selected item is always readable */
            QComboBox { color: #111827; background-color: #FFFFFF; selection-color: #FFFFFF; selection-background-color: #2563EB; }
            QComboBox QAbstractItemView {
                color: #111827;
                background-color: #FFFFFF;
                selection-color: #FFFFFF;
                selection-background-color: #2563EB;
                outline: none;
            }
            QComboBox QAbstractItemView::item { color: #111827; padding: 4px 8px; }
            QComboBox QAbstractItemView::item:selected { color: #FFFFFF; background-color: #2563EB; }
            QComboBox QAbstractItemView::item:hover   { color: #111827; background-color: #DBEAFE; }
            #ExitBtn { border: none; border-radius: 4px; background: transparent; padding: 8px; }
            #ExitBtn:hover { background-color: #7F1D1D; }
        """)

    def _build_sub_module_panel_widgets(self):
        self.sub_module_title = QLabel("Navigation")
        self.sub_module_title.setObjectName("SubModulePanelTitle")
        self.sub_module_title.setAlignment(Qt.AlignCenter)
        
        self.sub_module_button_container = QWidget()
        self.sub_module_button_layout = QVBoxLayout(self.sub_module_button_container)
        self.sub_module_button_layout.setContentsMargins(0, 0, 0, 0)
        self.sub_module_button_layout.setSpacing(8)
        self.sub_module_button_layout.addStretch(1)
        
        self.sub_module_panel_layout.addWidget(self.sub_module_title)
        self.sub_module_panel_layout.addWidget(self.sub_module_button_container, 1)

    def _build_permanent_icon_bar(self):
        toggle_btn = QToolButton()
        toggle_btn.setIcon(IconCache.get_icon("menu"))
        toggle_btn.setIconSize(QSize(24, 24))
        toggle_btn.setToolTip("Toggle Navigation Panel")
        toggle_btn.clicked.connect(self._toggle_sub_module_panel)
        self.permanent_icon_bar_layout.addWidget(toggle_btn)
        
        self.permanent_icon_bar_layout.addSpacing(15)
        self.icon_button_map = {}
        
        for mod in self.modules:
            btn = QToolButton()
            btn.setIcon(mod.icon or QIcon())
            btn.setIconSize(QSize(28, 28))
            btn.setToolTip(mod.title)
            btn.setCheckable(True)
            btn.clicked.connect(partial(self._on_main_icon_clicked, mod))
            self.permanent_icon_bar_layout.addWidget(btn)
            self.icon_button_map[mod.key] = btn
            
        self.permanent_icon_bar_layout.addStretch(1)
        
        log_btn = QToolButton()
        log_btn.setIcon(IconCache.get_icon("log", color=ICON_COLORS["settings"])) 
        log_btn.setIconSize(QSize(28, 28))
        log_btn.setToolTip("View Application Log")
        log_btn.clicked.connect(self.view_log_window)
        self.permanent_icon_bar_layout.addWidget(log_btn)
        self.permanent_icon_bar_layout.addSpacing(5)

        if self.settings_spec:
            settings_btn = QToolButton()
            settings_btn.setIcon(self.settings_spec.icon or QIcon())
            settings_btn.setIconSize(QSize(28, 28))
            settings_btn.setToolTip(self.settings_spec.title)
            settings_btn.setCheckable(True)
            settings_btn.clicked.connect(partial(self._on_main_icon_clicked, self.settings_spec))
            self.permanent_icon_bar_layout.addWidget(settings_btn)
            self.icon_button_map[self.settings_spec.key] = settings_btn

        self.permanent_icon_bar_layout.addSpacing(8)
        signout_btn = QToolButton()
        signout_btn.setIcon(IconCache.get_icon("system-users", color=QColor("#F59E0B")))
        signout_btn.setIconSize(QSize(24, 24))
        signout_btn.setToolTip("Sign Out")
        signout_btn.setObjectName("SignOutBtn")
        signout_btn.clicked.connect(self._confirm_signout)
        self.permanent_icon_bar_layout.addWidget(signout_btn)

        exit_btn = QToolButton()
        exit_btn.setIcon(IconCache.get_icon("exit", color=QColor("#EF4444")))
        exit_btn.setIconSize(QSize(26, 26))
        exit_btn.setToolTip("Exit IsoWorks pyLIMS")
        exit_btn.setObjectName("ExitBtn")
        exit_btn.clicked.connect(self._confirm_exit)
        self.permanent_icon_bar_layout.addWidget(exit_btn)
        self.permanent_icon_bar_layout.addSpacing(6)

    def view_log_window(self):
        """Finds the most recent log file and shows the last 1000 lines."""
        try:
            log_files = glob.glob("*.log")
            if not log_files:
                QMessageBox.information(self, "Log Viewer", "No log files found in current directory.")
                return
            
            # Sort by modification time
            latest_log = max(log_files, key=os.path.getmtime)
            
            with open(latest_log, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                
            tail = "".join(lines[-1000:]) # Last 1000 lines
            
            dlg = QDialog(self)
            dlg.setWindowTitle(f"Log Viewer - {latest_log}")
            dlg.resize(1200, 900)
            layout = QVBoxLayout(dlg)
            
            text_edit = QTextEdit()
            text_edit.setReadOnly(True)
            text_edit.setFont(QFont("Consolas", 10))
            text_edit.setPlainText(tail)
            # Scroll to bottom
            text_edit.moveCursor(text_edit.textCursor().End)
            layout.addWidget(text_edit)
            
            btn_close = QPushButton("Close")
            btn_close.clicked.connect(dlg.accept)
            layout.addWidget(btn_close)
            
            dlg.exec_()
            
        except Exception as e:
            QMessageBox.warning(self, "Log Error", f"Could not open log: {e}")

    def _toggle_sub_module_panel(self):
        self.sub_module_panel.setVisible(not self.sub_module_panel.isVisible())

    def _on_main_icon_clicked(self, mod: ModuleSpec):
        for key, btn in self.icon_button_map.items():
            if key != mod.key:
                btn.setChecked(False)
        self.icon_button_map[mod.key].setChecked(True)
        self.current_top_module_key = mod.key

        if not mod.children:
            self.sub_module_panel.setVisible(False)
            self._on_sidebar_navigation(mod.key)
            return

        panel_open = self.sub_module_panel.isVisible()
        same_module = (self.sub_module_title.text() == mod.title)

        if panel_open and same_module:
            # Collapse: clicking the same parent while its panel is open
            self.sub_module_panel.setVisible(False)
            self.icon_button_map[mod.key].setChecked(False)
        elif not panel_open and same_module:
            # Re-expand: panel was collapsed (e.g. after navigating to a child)
            # Just show it — keep the current page and active button state
            self.sub_module_panel.setVisible(True)
        else:
            # First time or switching from a different module:
            # Rebuild buttons and navigate to the first child
            self._populate_sub_module_panel(mod)
            self.sub_module_panel.setVisible(True)

    def _populate_sub_module_panel(self, mod: ModuleSpec):
        self.sub_module_title.setText(mod.title)

        # Clear existing buttons
        for i in reversed(range(self.sub_module_button_layout.count())):
            item = self.sub_module_button_layout.takeAt(i)
            if item.widget():
                item.widget().deleteLater()
        
        is_settings_module = (mod.key == "settings")
        
        # Recreate child buttons
        for child in mod.children:
            btn = QPushButton(child.icon or QIcon(), f" {child.title}")
            btn.setProperty("moduleKey", child.key)
            
            btn.clicked.connect(partial(self._on_sidebar_navigation, child.key, False))
            self.sub_module_button_layout.addWidget(btn)

        self.sub_module_button_layout.addStretch(1)

        # Navigate to the first child and mark it active
        if mod.children:
            first_key = mod.children[0].key
            self._ensure_widget_loaded(first_key)
            page_widget = self.key_to_widget_map.get(first_key)
            if page_widget:
                self.stack.setCurrentWidget(page_widget)
            if not is_settings_module:
                self._update_sub_module_active_button(first_key)

    def _load_connection_from_settings(self):
        try:
            dialect = self.settings.value("db/dialect", type=str)
            path_or_dsn = None
            
            if dialect == "ACCESS":
                path_or_dsn = self.settings.value("db/access_path", type=str)
            elif dialect == "SQL_SERVER":
                raw = self.settings.value("db/dsn_path", type=str)
                if raw:
                    path_or_dsn = raw.split(';')[0].strip()
            elif dialect == "POSTGRESQL":
                host = self.settings.value("db/pg_host", type=str)
                port = self.settings.value("db/pg_port", type=str)
                db = self.settings.value("db/pg_db", type=str)
                user = self.settings.value("db/pg_user", type=str)
                password = self.settings.value("db/pg_password", type=str)
                if all([host, port, db, user]):
                    if password:
                        path_or_dsn = f"postgresql://{user}:{password}@{host}:{port}/{db}"
                    else:
                        path_or_dsn = f"postgresql://{user}@{host}:{port}/{db}"

            if dialect and path_or_dsn:
                logging.info(f"Initializing DB Engine: {dialect} -> {path_or_dsn}")
                db_manager.initialize(dialect, path_or_dsn)
            else:
                logging.warning("No database settings found. Please configure in Settings.")
                
        except Exception as e:
            logging.error(f"Failed to initialize database: {e}", exc_info=True)

    def _build_menu(self):
        mb = self.menuBar()
        m_file = mb.addMenu("&File")
        act_quit = QAction("Quit", self)
        act_quit.triggered.connect(self._confirm_exit)
        m_file.addAction(act_quit)

    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(Qt.TopToolBarArea, tb)
        tb.setVisible(False)

    def _build_module_registry(self):
        # --- Settings (top-level) ---
        self.settings_spec = ModuleSpec(
            key="settings",
            title="Settings",
            icon=IconCache.get_icon("settings", color=ICON_COLORS["settings"]),
            description="Global settings and administrative tools.",
            children=[
                ModuleSpec(
                    key="employee_mgmt",
                    title="Employee Management",
                    icon=IconCache.get_icon("system-users", color=ICON_COLORS["settings"]),
                    description="Add, edit, or remove employees and their roles.",
                    make_embedded_widget=load_employee_mgmt
                ),
                ModuleSpec(
                    key="customer_mgmt",
                    title="Customer Management",
                    icon=IconCache.get_icon("x-office-address-book", color=ICON_COLORS["settings"]),
                    description="Add, edit, or remove customers (clients).",
                    make_embedded_widget=load_customer_mgmt
                ),
                ModuleSpec(
                    key="equipment_mgmt",
                    title="Equipment Management",
                    icon=IconCache.get_icon("computer", color=ICON_COLORS["settings"]),
                    description="Manage lab equipment, instruments, and devices.",
                    make_embedded_widget=load_equipment_mgmt
                ),
                ModuleSpec(
                    key="procedure_mgmt",
                    title="Procedure Management",
                    icon=IconCache.get_icon("workflow", color=ICON_COLORS["settings"]),
                    description="Manage analysis procedures and their configurations.",
                    make_embedded_widget=load_procedure_mgmt
                ),
                ModuleSpec(
                    key="workflow_mgmt",
                    title="Workflow Management",
                    icon=IconCache.get_icon("workflow", color=ICON_COLORS["settings"]),
                    description="Manage workflow templates and job lists.",
                    make_embedded_widget=load_workflow_mgmt
                ),
                ModuleSpec(
                    key="reference_mgmt",
                    title="References & Controls",
                    icon=IconCache.get_icon("calibration", color=ICON_COLORS["settings"]),
                    description="Manage reference materials and control standards.",
                    make_embedded_widget=load_reference_mgmt
                ),
                ModuleSpec(
                    key="cims",
                    title="Consumables Inventory",
                    icon=IconCache.get_icon("laboratory", color=ICON_COLORS["settings"]),
                    description="Manage lab consumables, lot tracking, usage traceability, and stock alerts.",
                    make_embedded_widget=load_cims
                ),
                ModuleSpec(
                    key="global_params",
                    title="Global Parameters",
                    icon=IconCache.get_icon("configure", color=ICON_COLORS["settings"]),
                    description="Edit application-wide key-value settings.",
                    make_embedded_widget=load_global_params
                ),
                ModuleSpec(
                    key="db_connection",
                    title="Database Connection",
                    icon=IconCache.get_icon("database", color=ICON_COLORS["settings"]),
                    description="Configure the main database connection.",
                    make_embedded_widget=load_settings_widget
                ),
                ModuleSpec(
                    key="audit_trail",
                    title="Audit Trail",
                    icon=IconCache.get_icon("history", color=ICON_COLORS["settings"]),
                    description="Browse the immutable change history for all audited tables.",
                    make_embedded_widget=load_audit_viewer
                ),
            ]
        )

        # --- Main Modules (top-level) ---
        self.modules = [
            ModuleSpec(
                key="dashboard",
                title="Dashboard",
                icon=IconCache.get_icon("dashboard", color=ICON_COLORS["settings"]),
                description="Main dashboard and lab status.",
                make_embedded_widget=load_dashboard
            ),
            ModuleSpec(
                key="sample_mgmt",
                title="Sample Management",
                icon=IconCache.get_icon("sample-management", color=ICON_COLORS["sample"]),
                description="Manage submissions and import samples.",
                children=[
                    ModuleSpec(
                        key="import_submission",
                        title="Import New Submission",
                        icon=IconCache.get_icon("document-new", color=ICON_COLORS["action"]),
                        description="Import samples from Excel, GNIP, or manual entry.",
                        make_embedded_widget=load_sample_submission
                    ),
                    ModuleSpec(
                        key="submission_list",
                        title="Submission Management",
                        icon=IconCache.get_icon("view-list-details", color=ICON_COLORS["list"]),
                        description="View, edit, and manage all submissions.",
                        make_embedded_widget=load_project_list
                    ),
                    ModuleSpec(
                        key="workflow_assign",
                        title="Stage Samples to TBA",
                        icon=IconCache.get_icon("workflow", color=ICON_COLORS["action"]),
                        description="Assign workflows and stage samples for analysis.",
                        make_embedded_widget=load_workflow_assign
                    ),
                    ModuleSpec(
                        key="samples_tba",
                        title="Manage Analysis Queue",
                        icon=IconCache.get_icon("format-list-ordered", color=ICON_COLORS["list"]),
                        description="View and manage samples waiting for analysis (TBA).",
                        make_embedded_widget=load_samples_tba_management
                    ),
                ]
            ),
            ModuleSpec(
                key="siam",
                title="Stable Isotopes (SIAM)",
                icon=IconCache.get_icon("stable-isotopes", color=ICON_COLORS["siam"]),
                description="Process and analyze stable isotope data.",
                children=[
                    ModuleSpec(
                        key="siam_run_list",
                        title="SIAM Runs",
                        icon=IconCache.get_icon("view-list-details", color=ICON_COLORS["siam"]),
                        description="View, manage, and process existing LIMS runs.",
                        make_embedded_widget=load_siam_run_list
                    ),
                    ModuleSpec(
                        key="siam_preanalysis",
                        title="Pre-Analysis Batches",
                        icon=IconCache.get_icon("chemistry", color=ICON_COLORS["siam"]),
                        description="Manage Major Ions / Chemistry pre-analysis batches (NOx threshold screening).",
                        make_embedded_widget=load_siam_preanalysis
                    ),
                    ModuleSpec(
                        key="create_siam_run_processor",
                        title="Process Run (File-Based)",
                        icon=IconCache.get_icon("analysis", color=ICON_COLORS["siam"]),
                        description="Launch the data processor for a file-based run.",
                        make_embedded_widget=load_processor_widget
                    ),

                ]
            ),
            ModuleSpec(
                key="trims",
                title="Tritium Analysis (TRIMS)",
                icon=IconCache.get_icon("tritium", color=ICON_COLORS["trims"]),
                description="TRIMS pipeline for Tritium Analysis.",
                children=[
                    ModuleSpec(
                        key="trims_distillation",
                        title="Primary Distillation",
                        icon=IconCache.get_icon("distillation", color=ICON_COLORS["trims"]),
                        description="Manage Tritium Primary Distillation Batches.",
                        make_embedded_widget=load_trims_distillation
                    ),
                    ModuleSpec(
                        key="trims_enrichment",
                        title="Electrolytic Enrichment",
                        icon=IconCache.get_icon("enrichment", color=ICON_COLORS["trims"]),
                        description="Manage Electrolytic Enrichment Runs.",
                        make_embedded_widget=load_trims_electrolysis
                    ),
                    ModuleSpec(
                        key="trims_chem_enrichment",
                        title="Chemical Enrichment",
                        icon=IconCache.get_icon("enrichment", color=ICON_COLORS["trims"]),
                        description="Manage Chemical Pre-concentration Runs (35S, 14C, …).",
                        make_embedded_widget=load_trims_chem_enrichment
                    ),
                    ModuleSpec(
                        key="trims_lsc",
                        title="LSC Runs",
                        icon=IconCache.get_icon("lsc-counter", color=ICON_COLORS["trims"]),
                        description="Manage LSC Runs.",
                        make_embedded_widget=load_trims_lsc_runs
                    ),
                ]
            ),
            ModuleSpec(
                key="ngam",
                title="Noble Gas (NGAM)",
                icon=IconCache.get_icon("tritium", color=ICON_COLORS["ngam"]),
                description="Noble Gas Analysis Module — 3H-3He ingrowth, extraction and measurement.",
                children=[
                    ModuleSpec(
                        key="ngam_ingrowth",
                        title="3He Ingrowth Runs",
                        icon=IconCache.get_icon("radioactive", color=ICON_COLORS["ngam"]),
                        description="Manage 3H-3He ingrowth degassing runs.",
                        make_embedded_widget=load_ngam_ingrowth_runs
                    ),
                    ModuleSpec(
                        key="ngam_sequence",
                        title="3He Measurement Runs",
                        icon=IconCache.get_icon("analysis", color=ICON_COLORS["ngam"]),
                        description="Manage noble gas mass-spectrometer measurement sequences.",
                        make_embedded_widget=load_ngam_sequence_runs
                    ),
                    ModuleSpec(
                        key="ngam_extraction",
                        title="NG Extraction Runs",
                        icon=IconCache.get_icon("enrichment", color=ICON_COLORS["ngam"]),
                        description="Manage 3He gas extraction runs.",
                        make_embedded_widget=load_ngam_extraction_runs
                    ),                    
                    ModuleSpec(
                        key="ngam_ng_sequence",
                        title="NG MS Sequence Runs",
                        icon=IconCache.get_icon("analysis", color=ICON_COLORS["ngam"]),
                        description="Manage noble gas (non-ingrowth) MS sequence runs.",
                        make_embedded_widget=load_ngam_ng_sequence_runs
                    ),
                    ModuleSpec(
                        key="ngam_import_protocol",
                        title="Import from Protocol",
                        icon=IconCache.get_icon("database", color=ICON_COLORS["ngam"]),
                        description="Import a raw LabVIEW .protocol sequence into the database.",
                        make_embedded_widget=load_ngam_import_from_protocol
                    ),
                    ModuleSpec(
                        key="ngam_eqw_cf",
                        title="EQW Correction Factors",
                        icon=IconCache.get_icon("calibrate", color=ICON_COLORS["ngam"]),
                        description="Manage EQW calibration runs and correction-factor templates.",
                        make_embedded_widget=load_ngam_eqw_cf
                    ),
                ]
            ),
            ModuleSpec(
                key="ams",
                title="AMS — ¹⁴C",
                icon=IconCache.get_icon("radioactive", color=ICON_COLORS["ams"]),
                description="Accelerator Mass Spectrometry — ¹⁴C measurement, data import and reduction.",
                children=[
                    ModuleSpec(
                        key="ams_physical_prep",
                        title="Physical Prep",
                        icon=IconCache.get_icon("batch", color=ICON_COLORS["ams"]),
                        description="Mechanical sample preparation (cleaning, sieving, crushing) before pretreatment.",
                        make_embedded_widget=load_physical_prep
                    ),
                    ModuleSpec(
                        key="ams_pretreatment",
                        title="Pretreatment",
                        icon=IconCache.get_icon("chemistry", color=ICON_COLORS["ams"]),
                        description="Chemical pretreatment (ABA and other methods) to remove contaminants before purification.",
                        make_embedded_widget=load_pretreatment
                    ),
                    ModuleSpec(
                        key="ams_purification",
                        title="Purification",
                        icon=IconCache.get_icon("stock_convert", color=ICON_COLORS["ams"]),
                        description="CO₂ purification before graphitisation.",
                        make_embedded_widget=load_purification
                    ),
                    ModuleSpec(
                        key="ams_graphitisation",
                        title="Graphitisation",
                        icon=IconCache.get_icon("flask", color=ICON_COLORS["ams"]),
                        description="Prepare samples by graphitisation (CO₂ → graphite) before AMS measurement.",
                        make_embedded_widget=load_graphitisation
                    ),
                    ModuleSpec(
                        key="ams_run_list",
                        title="AMS Runs",
                        icon=IconCache.get_icon("view-list-details", color=ICON_COLORS["ams"]),
                        description="View, create, and manage AMS ¹⁴C runs.",
                        make_embedded_widget=load_ams_run_list
                    ),
                ]
            ),
            ModuleSpec(
                key="qaqc",
                title="QA/QC Module",
                icon=IconCache.get_icon("qa-qc", color=ICON_COLORS["qaqc"]),
                description="Cross-run QC dashboards, control charts.",
                make_embedded_widget=load_qaqc
            ),
        ]

    def _clear_stack(self):
        for i in reversed(range(self.stack.count())):
            w = self.stack.widget(i)
            self.stack.removeWidget(w)
            w.deleteLater()

    def _inflate_modules_metadata(self):
        """
        Populate internal maps (spec registry) without instantiating widgets.
        This enables Lazy Loading.
        """
        self._clear_stack()
        self.key_to_widget_map = {}
        self.key_to_spec_map = {}

        def register_spec(spec):
            self.key_to_spec_map[spec.key] = spec

        for mod in self.modules:
            if mod.children:
                for child in mod.children:
                    register_spec(child)
            else:
                register_spec(mod)
        
        if self.settings_spec:
            for child in self.settings_spec.children:
                register_spec(child)

        self._build_permanent_icon_bar()

    def _ensure_widget_loaded(self, key: str) -> Optional[QWidget]:
        """Lazy Loader: Checks if widget exists, if not creates it."""
        if key in self.key_to_widget_map:
            return self.key_to_widget_map[key]

        spec = self.key_to_spec_map.get(key)
        if not spec:
            logging.error(f"Cannot find spec for key: {key}")
            return None

        logging.info(f"Lazy Loading Module: {spec.title}")
        widget = self._create_module_widget(spec)
        self.stack.addWidget(widget)

        widget.style().polish(widget)

        self.key_to_widget_map[key] = widget
        return widget

    def _create_module_widget(self, mod: ModuleSpec) -> QWidget:
        if mod.make_embedded_widget:
            try:
                return mod.make_embedded_widget()
            except Exception as e:
                logging.error(f"Failed to create widget for {mod.key}: {e}", exc_info=True)
                return ComingSoonWidget(mod.title, f"Error loading module: {e}")
        elif mod.open_external:
            return ComingSoonWidget(mod.title, "External Tool")
        else:
            return ComingSoonWidget(mod.title, mod.description)

    def _update_sub_module_active_button(self, key: str):
        for i in range(self.sub_module_button_layout.count()):
            widget = self.sub_module_button_layout.itemAt(i).widget()
            if isinstance(widget, QPushButton):
                is_active = (widget.property("moduleKey") == key)
                widget.setProperty("activeModule", is_active)
                widget.style().unpolish(widget)
                widget.style().polish(widget)

    def _on_sidebar_navigation(self, key: str, collapse_after: bool = False):
        logging.info(f"Navigating to module: {key}")

        # LAZY LOAD: Create the widget only now
        page_widget = self._ensure_widget_loaded(key)
        if page_widget:
            self.stack.setCurrentWidget(page_widget)
            self._update_sub_module_active_button(key)
            self._run_refresh_logic(key, page_widget)

            # Collapse the sub-module panel ONLY when this was a real second-level click
            if collapse_after and self.sub_module_panel.isVisible():
                self.sub_module_panel.setVisible(False)

            self.settings.setValue("launcher/last_key", key)
        else:
            logging.warning(f"No widget found for key: {key}")

    def _run_refresh_logic(self, key: str, page_widget: QWidget):
        """
        Refreshes the module's data. 
        Modified to ONLY auto-load the Dashboard. 
        Other modules start on a 'blank canvas' to prevent startup delays.
        """
        # 1. Dashboard: Auto-refresh
        if key == "dashboard":
            if hasattr(page_widget, "refresh_dashboard"):
                try:
                    page_widget.refresh_dashboard()
                except Exception as e:
                    logging.error(f"Error refreshing dashboard: {e}")
            elif hasattr(page_widget, "refresh") and callable(page_widget.refresh):
                try:
                    page_widget.refresh()
                except Exception as e:
                    logging.error(f"Error refreshing dashboard: {e}")
            return

    def _handle_global_help(self):
        """
        Invoked when F1 is pressed globally. Finds the currently active widget 
        in the stack, looks up its ModuleSpec key, and opens the help browser.
        """
        current_w = self.stack.currentWidget()
        active_key = "dashboard" # Fallback
        
        if current_w:
            # Reverse lookup: find the key corresponding to this exact widget instance
            for key, widget in self.key_to_widget_map.items():
                if widget is current_w:
                    active_key = key
                    break
                    
        show_help(self, active_key)

        # 2. Other Modules: Do nothing.
        # We deliberately skip calling load_project_list(), load_run_list(), etc.
        pass

    def _find_parent_mod(self, child_key: str) -> Optional[ModuleSpec]:
        for mod in self.modules:
            if mod.key == child_key: return mod
            if mod.children:
                for child in mod.children:
                    if child.key == child_key: return mod
        if self.settings_spec and self.settings_spec.children:
            for child in self.settings_spec.children:
                if child.key == child_key: return self.settings_spec
        return None

    def _restore_ui_state(self):
        try:
            geom = self.settings.value("launcher/geometry")
            if geom: self.restoreGeometry(geom)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        
        try:
            # FORCE Dashboard as default if no key is saved to ensure light startup
            last_key = self.settings.value("launcher/last_key", "dashboard")
            
            parent_mod = self._find_parent_mod(last_key)
            
            if parent_mod:
                self.current_top_module_key = parent_mod.key
                self._on_main_icon_clicked(parent_mod)
                
                # If it's a grouped module, ensure we activate the specific child
                if parent_mod.children:
                    # Note: _on_main_icon_clicked calls _populate_sub_module_panel
                    # which lazily creates the FIRST child. If last_key was a different child,
                    # we need to navigate to it explicitly now.
                    if parent_mod.children[0].key != last_key:
                        self._on_sidebar_navigation(last_key)
            else:
                # Fallback to Dashboard
                if self.modules:
                    self._on_main_icon_clicked(self.modules[0])
                    
        except Exception as e:
            logging.error(f"Failed to restore UI state: {e}")

    def show_home(self):
        """Navigate to the first (default) module — used by embedded widgets."""
        if self.modules:
            self._on_main_icon_clicked(self.modules[0])

    def _confirm_signout(self):
        reply = QMessageBox.question(
            self,
            "Sign Out",
            "Are you sure you want to sign out of IsoWorks pyLIMS?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            try:
                self.settings.setValue("launcher/geometry", self.saveGeometry())
            except Exception as exc:
                logging.warning(f"Exception saving geometry: {exc}")
            self._allow_close = True
            QApplication.quit()

    def _confirm_exit(self):
        reply = QMessageBox.question(
            self,
            "Exit IsoWorks pyLIMS",
            "Are you sure you want to exit IsoWorks pyLIMS?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            try:
                self.settings.setValue("launcher/geometry", self.saveGeometry())
            except Exception as exc:
                logging.warning(f"Exception saving geometry: {exc}")
            self._allow_close = True
            QApplication.quit()

    def closeEvent(self, e):
        if getattr(self, "_allow_close", False):
            super().closeEvent(e)
        else:
            e.ignore()
            self._confirm_exit()

def main():
    # Required before QApplication for QtWebEngine on macOS
    if hasattr(Qt, 'AA_ShareOpenGLContexts'):
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    # Enable High DPI scaling before creating the QApplication
    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    # Avoid macOS file dialog hangs/freezes by using Qt non-native dialogs
    if hasattr(Qt, 'AA_DontUseNativeDialogs'):
        QApplication.setAttribute(Qt.AA_DontUseNativeDialogs, True)

    logging.basicConfig(
        # level=logging.DEBUG, 
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
        handlers=[
            logging.FileHandler("lims_launcher.log", mode='w'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    logging.info("Starting LIMS Launcher Application")
    
    app = QApplication(sys.argv)
    app.setApplicationName(AppShell.APP)
    app.setOrganizationName(AppShell.ORG)
    
    # Set Fusion style for consistent cross-platform appearance
    app.setStyle('Fusion')

    win = AppShell()
    win.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()