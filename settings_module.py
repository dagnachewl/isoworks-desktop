"""
settings_module.py — Application settings and database connection panel for IsoWorks.
Provides SettingsWidget for selecting the database dialect (PostgreSQL, SQL Server,
MS Access), entering credentials, and persisting them via QSettings.
"""
import logging
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QGroupBox, QRadioButton, QHBoxLayout,
    QFormLayout, QLineEdit, QPushButton, QFileDialog, QMessageBox,
    QComboBox, QLabel, QDoubleSpinBox
)
from PyQt5.QtCore import QSettings

# --- NEW: Import the shared database manager ---
from db_core import db_manager
from sqlalchemy import text
from help_browser import make_help_button

_PRINT_SETTINGS_ORG = "YourOrg"
_PRINT_SETTINGS_APP = "ISOTOPES_SUITE"

_PAGE_SIZES = {"A4": "A4", "A3": "A3", "Letter": "Letter", "Legal": "Legal"}
_ORIENTATIONS = ["Portrait", "Landscape"]


def apply_print_settings(printer) -> None:
    """Apply saved print defaults (printer name, page size, orientation) to a QPrinter."""
    try:
        from PyQt5.QtPrintSupport import QPrinter
        s = QSettings(_PRINT_SETTINGS_ORG, _PRINT_SETTINGS_APP)
        name = s.value("print/printer_name", "", type=str)
        if name:
            printer.setPrinterName(name)
        sizes = {"A4": QPrinter.A4, "A3": QPrinter.A3,
                 "Letter": QPrinter.Letter, "Legal": QPrinter.Legal}
        printer.setPageSize(sizes.get(s.value("print/page_size", "A4", type=str), QPrinter.A4))
        printer.setOrientation(
            QPrinter.Landscape
            if s.value("print/orientation", "Portrait", type=str) == "Landscape"
            else QPrinter.Portrait
        )
    except Exception as exc:
        logging.warning("apply_print_settings: %s", exc)


class PrinterSettingsGroup(QGroupBox):
    """Inline group for default printer / page settings, embedded in SettingsWidget."""

    def __init__(self, settings: QSettings, parent=None):
        super().__init__("Print Defaults", parent)
        self.settings = settings

        form = QFormLayout(self)
        form.setSpacing(4)

        # Printer name
        self.cmbPrinter = QComboBox()
        self.cmbPrinter.addItem("(System Default)", "")
        try:
            from PyQt5.QtPrintSupport import QPrinterInfo
            for pi in QPrinterInfo.availablePrinters():
                self.cmbPrinter.addItem(pi.printerName(), pi.printerName())
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        form.addRow("Printer:", self.cmbPrinter)

        # Page size
        self.cmbPageSize = QComboBox()
        for name in _PAGE_SIZES:
            self.cmbPageSize.addItem(name, name)
        form.addRow("Page Size:", self.cmbPageSize)

        # Orientation
        self.cmbOrientation = QComboBox()
        for o in _ORIENTATIONS:
            self.cmbOrientation.addItem(o, o)
        form.addRow("Orientation:", self.cmbOrientation)

        # Page margin (single value applied to all 4 sides)
        self.spnMargin = QDoubleSpinBox()
        self.spnMargin.setRange(0.0, 50.0)
        self.spnMargin.setSingleStep(1.0)
        self.spnMargin.setSuffix(" mm")
        self.spnMargin.setValue(10.0)
        form.addRow("Page Margin:", self.spnMargin)

        self.btnSave = QPushButton("Save Print Settings")
        self.btnSave.clicked.connect(self._save)
        form.addRow("", self.btnSave)

        self._load()

    def _load(self):
        s = self.settings
        name = s.value("print/printer_name", "", type=str)
        idx = self.cmbPrinter.findData(name)
        if idx >= 0:
            self.cmbPrinter.setCurrentIndex(idx)

        idx = self.cmbPageSize.findData(s.value("print/page_size", "A4", type=str))
        if idx >= 0:
            self.cmbPageSize.setCurrentIndex(idx)

        idx = self.cmbOrientation.findData(s.value("print/orientation", "Portrait", type=str))
        if idx >= 0:
            self.cmbOrientation.setCurrentIndex(idx)

        self.spnMargin.setValue(float(s.value("print/margin", 10.0)))

    def _save(self):
        s = self.settings
        s.setValue("print/printer_name", self.cmbPrinter.currentData())
        s.setValue("print/page_size", self.cmbPageSize.currentData())
        s.setValue("print/orientation", self.cmbOrientation.currentData())
        s.setValue("print/margin", self.spnMargin.value())
        s.sync()
        QMessageBox.information(self, "Saved", "Print settings saved.")


class SettingsWidget(QWidget):
    """
    A widget for configuring application-wide settings.
    
    This module establishes the core connection between the IsoWorks PyQt frontend
    and the SQL database backend (`db_core.py`).
    
    Available Dialects:
    - PostgreSQL: Host, Port, Database, Username, Password
    - SQL Server: File DSN path (.dsn)
    - MS Access: File path to an .accdb or .mdb database file
    
    Operation:
    1. Select the database dialect.
    2. Provide the necessary connection strings or credentials.
    3. Click 'Test & Save Connection'. IsoWorks attempts a test query (`SELECT 1`).
    4. If successful, the dialect and encrypted credentials are automatically persisted
       to local OS `QSettings` (`db/dialect`, `db/pg_host`, etc.).
       
    Note: For PostgreSQL, the application utilizes SQLAlchemy connection pooling.
    A restart is recommended after changing the active database connection to clear
    cached queries and ensure all modules sync correctly.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Ensure we write to the same settings location as launcher.py
        self.settings = QSettings("YourOrg", "ISOTOPES_SUITE")
        
        main_layout = QVBoxLayout(self)
        
        # --- Help Button ---
        top_layout = QHBoxLayout()
        top_layout.addStretch()
        top_layout.addWidget(make_help_button(self, "db_connection"))
        main_layout.addLayout(top_layout)
        
        # --- Database Connection Group ---
        db_group = self._create_connection_group()
        main_layout.addWidget(db_group)

        # --- Printer Settings Group ---
        self._print_grp = PrinterSettingsGroup(self.settings, self)
        main_layout.addWidget(self._print_grp)

        main_layout.addStretch()
        self._load_settings()

    def _create_connection_group(self):
        group = QGroupBox("Database Connection")
        main_layout = QVBoxLayout()
        self.rb_conn_file = QRadioButton("MS Access File")
        self.rb_conn_dsn = QRadioButton("File DSN (SQL Server, etc)")
        self.rb_conn_postgres = QRadioButton("PostgreSQL")
        
        radio_layout = QHBoxLayout()
        radio_layout.addWidget(self.rb_conn_file)
        radio_layout.addWidget(self.rb_conn_dsn)
        radio_layout.addWidget(self.rb_conn_postgres)
        radio_layout.addStretch()
        main_layout.addLayout(radio_layout)
        
        input_layout = QFormLayout()
        self.file_conn_layout = QHBoxLayout()
        self.txt_conn_file = QLineEdit()
        self.btn_conn_browse = QPushButton("...")
        self.file_conn_layout.addWidget(self.txt_conn_file)
        self.file_conn_layout.addWidget(self.btn_conn_browse)
        input_layout.addRow("Access File:", self.file_conn_layout)
        
        self.dsn_conn_layout = QHBoxLayout() 
        self.txt_conn_dsn_file = QLineEdit() 
        self.btn_conn_browse_dsn = QPushButton("...") 
        self.dsn_conn_layout.addWidget(self.txt_conn_dsn_file)
        self.dsn_conn_layout.addWidget(self.btn_conn_browse_dsn)
        input_layout.addRow("File DSN:", self.dsn_conn_layout)
        
        # PostgreSQL connection fields
        self.postgres_conn_layout = QVBoxLayout()
        postgres_form = QFormLayout()
        self.txt_pg_host = QLineEdit()
        self.txt_pg_host.setText("localhost")
        self.txt_pg_port = QLineEdit()
        self.txt_pg_port.setText("5432")
        self.txt_pg_db = QLineEdit()
        self.txt_pg_db.setText("isoworks")
        self.txt_pg_user = QLineEdit()
        self.txt_pg_password = QLineEdit()
        self.txt_pg_password.setEchoMode(QLineEdit.Password)
        
        postgres_form.addRow("Host:", self.txt_pg_host)
        postgres_form.addRow("Port:", self.txt_pg_port)
        postgres_form.addRow("Database:", self.txt_pg_db)
        postgres_form.addRow("User:", self.txt_pg_user)
        postgres_form.addRow("Password:", self.txt_pg_password)
        self.postgres_conn_layout.addLayout(postgres_form)
        input_layout.addRow("PostgreSQL:", self.postgres_conn_layout)
        
        main_layout.addLayout(input_layout)
        
        self.btn_set_connection = QPushButton("Test & Save Connection")
        
        # Connect signals
        self.rb_conn_file.toggled.connect(self._on_connection_mode_changed)
        self.rb_conn_dsn.toggled.connect(self._on_connection_mode_changed)
        self.rb_conn_postgres.toggled.connect(self._on_connection_mode_changed)
        self.btn_conn_browse.clicked.connect(self._browse_for_db_file)
        self.btn_conn_browse_dsn.clicked.connect(self._browse_for_dsn_file)
        self.btn_set_connection.clicked.connect(self._set_connection)
        
        main_layout.addWidget(self.btn_set_connection)
        group.setLayout(main_layout)
        self._on_connection_mode_changed() # Set initial state
        return group
        
    def _on_connection_mode_changed(self):
        is_file = self.rb_conn_file.isChecked()
        is_dsn = self.rb_conn_dsn.isChecked()
        is_postgres = self.rb_conn_postgres.isChecked()
        
        self.txt_conn_file.setEnabled(is_file)
        self.btn_conn_browse.setEnabled(is_file)
        self.txt_conn_dsn_file.setEnabled(is_dsn)
        self.btn_conn_browse_dsn.setEnabled(is_dsn)
        self.txt_pg_host.setEnabled(is_postgres)
        self.txt_pg_port.setEnabled(is_postgres)
        self.txt_pg_db.setEnabled(is_postgres)
        self.txt_pg_user.setEnabled(is_postgres)
        self.txt_pg_password.setEnabled(is_postgres)

    def _browse_for_db_file(self):
        file_name, _ = QFileDialog.getOpenFileName(self, "Select MS Access Database", "", "Access Databases (*.accdb *.mdb)")
        if file_name:
            self.txt_conn_file.setText(file_name)

    def _browse_for_dsn_file(self):
        file_name, _ = QFileDialog.getOpenFileName(self, "Select File DSN", "", "Data Source Files (*.dsn)")
        if file_name:
            self.txt_conn_dsn_file.setText(file_name)

    def _load_settings(self):
        """
        Load connection info from QSettings (disk) into the UI.
        This ensures the UI reflects what will be loaded on startup.
        """
        dialect = self.settings.value("db/dialect", type=str)
        
        if dialect == "ACCESS":
            self.rb_conn_file.setChecked(True)
            self.txt_conn_file.setText(self.settings.value("db/access_path", type=str))
        elif dialect == "SQL_SERVER":
            self.rb_conn_dsn.setChecked(True)
            self.txt_conn_dsn_file.setText(self.settings.value("db/dsn_path", type=str))
        elif dialect == "POSTGRESQL":
            self.rb_conn_postgres.setChecked(True)
            self.txt_pg_host.setText(self.settings.value("db/pg_host", type=str))
            self.txt_pg_port.setText(self.settings.value("db/pg_port", type=str))
            self.txt_pg_db.setText(self.settings.value("db/pg_db", type=str))
            self.txt_pg_user.setText(self.settings.value("db/pg_user", type=str))
            self.txt_pg_password.setText(self.settings.value("db/pg_password", type=str))
            
        self._on_connection_mode_changed()

    def _set_connection(self):
        """
        Refactored to use db_manager.initialize() instead of manual string building.
        """
        logging.info("Attempting to set connection...")
        path = ""
        dialect = ""

        if self.rb_conn_file.isChecked():
            path = self.txt_conn_file.text().strip()
            dialect = "ACCESS"
            if not path:
                QMessageBox.warning(self, "Error", "Please select an MS Access file path.")
                return

        elif self.rb_conn_dsn.isChecked():
            path = self.txt_conn_dsn_file.text().strip()
            dialect = "SQL_SERVER"
            if not path:
                QMessageBox.warning(self, "Error", "Please select a File DSN path.")
                return
            # Clean up DSN path if needed (remove extra semicolons)
            path = path.split(';')[0].strip()

        elif self.rb_conn_postgres.isChecked():
            host = self.txt_pg_host.text().strip()
            port = self.txt_pg_port.text().strip()
            db = self.txt_pg_db.text().strip()
            user = self.txt_pg_user.text().strip()
            password = self.txt_pg_password.text().strip()
            
            if not all([host, port, db, user]):
                QMessageBox.warning(self, "Error", "Please fill in all required PostgreSQL connection fields (Host, Port, Database, User). Password is optional.")
                return
            
            # Build connection string for PostgreSQL
            if password:
                path = f"postgresql://{user}:{password}@{host}:{port}/{db}"
            else:
                path = f"postgresql://{user}@{host}:{port}/{db}"
            logging.info(f"PostgreSQL connection string: postgresql://{user}:***@{host}:{port}/{db}")
            logging.info(f"Password provided: {len(password) > 0}")
            dialect = "POSTGRESQL"

        try:
            # 1. Initialize the Core Engine
            logging.info(f"Initializing {dialect} with {path}")
            db_manager.initialize(dialect, path)
            
            # 2. Test the connection
            # We try to get a connection from the engine and run a simple query
            with db_manager.get_connection() as conn:
                conn.execute(text("SELECT 1"))
            
            logging.info("Connection successful.")
            
            # 3. Save to QSettings (Persist)
            self.settings.setValue("db/dialect", dialect)
            if dialect == "ACCESS":
                self.settings.setValue("db/access_path", path)
            elif dialect == "SQL_SERVER":
                self.settings.setValue("db/dsn_path", path)
            elif dialect == "POSTGRESQL":
                self.settings.setValue("db/pg_host", host)
                self.settings.setValue("db/pg_port", port)
                self.settings.setValue("db/pg_db", db)
                self.settings.setValue("db/pg_user", user)
                self.settings.setValue("db/pg_password", password)
            self.settings.sync()
            
            QMessageBox.information(self, "Success", "Database connection established and saved.")

        except Exception as e:
            logging.error(f"Connection failed: {e}", exc_info=True)
            QMessageBox.critical(self, "Connection Failed", 
                         f"Could not connect to the database.\n\nError: {e}")