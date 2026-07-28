from PyQt5.QtWidgets import (
    QWidget, QLabel, QFormLayout, QVBoxLayout, QHBoxLayout, QGroupBox, QGridLayout,
    QLineEdit, QPushButton, QComboBox, QAction, QCheckBox, QSpacerItem, QSizePolicy,
    QScrollArea, QFrame, QMessageBox, QFileDialog, QToolButton
)
from PyQt5.QtCore import Qt, QSize, QSignalBlocker
from PyQt5.QtGui import QIcon, QFont
from protocol_manager import ProtocolManager, Protocol
from isotope_processor import Config, InstrumentType
import os
import logging
from shared_utils import IconCache


class InputPanelBuilder:
    """
    Constructs the left sidebar (input panel) of ProcessorWidget.

    Usage
    -----
        self._input_builder = InputPanelBuilder(self)
        self._input_builder.setup_input_panel()

    All widgets are attached directly to ``self.parent`` so every existing
    reference in main_window.py (e.g. ``self.process_button``) keeps working
    without change.  The builder itself must be stored as an instance attribute
    of the parent so that signal connections to builder methods stay alive.
    """

    def __init__(self, parent):
        self.parent = parent

    # ------------------------------------------------------------------ #
    # Public entry point                                                   #
    # ------------------------------------------------------------------ #

    def setup_input_panel(self):
        p = self.parent   # shorthand — p is always the ProcessorWidget

        # Root panel — outer QVBoxLayout: scroll area (top) + sticky footer (bottom)
        p.input_panel = QWidget(p)
        p.input_panel.setObjectName("InputDockBody")
        outer_layout = QVBoxLayout(p.input_panel)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # Scrollable config area
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        _scroll.setFrameShape(QFrame.NoFrame)
        _scroll.setObjectName("InputPanelScroll")
        _config_widget = QWidget()
        _config_widget.setObjectName("InputDockBody")
        outer_layout.addWidget(_scroll, 1)
        _scroll.setWidget(_config_widget)

        # Separator line between scroll area and sticky footer
        _sep = QFrame()
        _sep.setFrameShape(QFrame.HLine)
        _sep.setFrameShadow(QFrame.Sunken)
        outer_layout.addWidget(_sep, 0)

        # Sticky footer — Process, Save, Status, Actions always visible
        p._input_footer = QWidget()
        p._input_footer.setObjectName("InputDockBody")
        p._footer_layout = QVBoxLayout(p._input_footer)
        p._footer_layout.setContentsMargins(4, 4, 4, 4)
        p._footer_layout.setSpacing(2)
        outer_layout.addWidget(p._input_footer, 0)

        # QFormLayout on the scrollable config widget
        p.input_layout = QFormLayout(_config_widget)
        p.input_layout.setHorizontalSpacing(8)
        p.input_layout.setVerticalSpacing(2)
        p.input_layout.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        p.input_layout.setFormAlignment(Qt.AlignTop)
        p.input_layout.setContentsMargins(4, 4, 4, 2)

        def _full_row(widget_or_layout):
            p.input_layout.addRow(widget_or_layout)

        # --- 0) Header title
        title_label = QLabel("Input Panel", p.input_panel)
        title_label.setObjectName("InputPanelTitle")
        title_label.setAlignment(Qt.AlignCenter)
        _full_row(title_label)

        # --- 1) Instrument
        instr_row = QHBoxLayout()
        instr_row.setContentsMargins(0, 0, 0, 0)
        instr_row.addWidget(QLabel("Instrument:", p.input_panel))
        if not hasattr(p, "instrument_combo"):
            p.instrument_combo = QComboBox(p.input_panel)
            p.instrument_combo.addItems(["LGR", "Picarro", "IRMS (Thermo DI)", "IRMS (EA)"])
            p.instrument_combo.currentTextChanged.connect(p.on_instrument_changed)
        instr_row.addWidget(p.instrument_combo, 1)
        p.input_layout.addRow(instr_row)

        # --- 1b) File Format
        if not hasattr(p, "cmbFileFormat"):
            p.cmbFileFormat = QComboBox(p.input_panel)
            p.cmbFileFormat.setToolTip("Select instrument file format (sets instrument automatically)")
            p.cmbFileFormat.currentIndexChanged.connect(self._on_format_changed)
            self._populate_file_formats()
            p.cmbFileFormat.setEnabled(True)
        p.input_layout.addRow("File Format:", p.cmbFileFormat)

        # --- 2) Option
        opt_row = QHBoxLayout()
        opt_row.setContentsMargins(0, 0, 0, 0)
        opt_row.addWidget(QLabel("Option:", p.input_panel))
        if not hasattr(p, "option_combo"):
            p.option_combo = QComboBox(p.input_panel)
            p.option_combo.addItems(["File-Based"])
            p.option_combo.currentTextChanged.connect(p.on_option_changed)
        p._rebuild_option_items()
        opt_row.addWidget(p.option_combo, 1)
        p.input_layout.addRow(opt_row)

        # --- 3) Files / LIMS group
        p.files_group = QGroupBox("Files", p.input_panel)
        fg = QVBoxLayout(p.files_group)
        fg.setContentsMargins(4, 8, 4, 2)
        fg.setSpacing(2)

        data_label_row = QHBoxLayout()
        data_label_row.setContentsMargins(0, 0, 0, 0)
        data_label_row.addWidget(QLabel("Data:", p.files_group))
        data_label_row.addStretch(1)
        fg.addLayout(data_label_row)

        data_row = QHBoxLayout()
        data_row.setContentsMargins(0, 0, 0, 0)
        if not hasattr(p, "data_file_edit"):
            p.data_file_edit = QLineEdit(p.files_group)
        if not hasattr(p, "data_browse_btn"):
            p.data_browse_btn = QPushButton("…", p.files_group)
            p.data_browse_btn.clicked.connect(p.select_data_file)
        data_row.addWidget(p.data_file_edit, 1)
        data_row.addWidget(p.data_browse_btn)
        fg.addLayout(data_row)

        standards_label_col = QVBoxLayout()
        standards_label_col.setContentsMargins(0, 0, 0, 0)
        standards_label_col.setSpacing(5)
        standards_label_col.addWidget(QLabel("Standards:", p.files_group))
        fg.addLayout(standards_label_col)

        std_row = QHBoxLayout()
        std_row.setContentsMargins(0, 0, 0, 0)
        if not hasattr(p, "standards_browse_btn"):
            p.standards_browse_btn = QPushButton("…", p.files_group)
            p.standards_browse_btn.clicked.connect(p.select_standards_file)
        if not hasattr(p, "standards_file_edit"):
            p.standards_file_edit = QLineEdit(p.files_group)
        std_row.addWidget(p.standards_file_edit, 1)
        std_row.addWidget(p.standards_browse_btn)
        fg.addLayout(std_row)

        if not hasattr(p, "load_both_btn"):
            p.load_both_btn = QPushButton("Load Data & Standards", p.files_group)
            p.load_both_btn.setEnabled(False)
            p.load_both_btn.clicked.connect(p._load_data_and_standards_from_textboxes)
        fg.addWidget(p.load_both_btn)

        # Wire enabling logic (guard against duplicate connects)
        try:
            try:
                p.data_file_edit.textChanged.disconnect(p._update_load_both_enabled)
            except Exception:
                pass
            p.data_file_edit.textChanged.connect(p._update_load_both_enabled)
            try:
                p.standards_file_edit.textChanged.disconnect(p._update_load_both_enabled)
            except Exception:
                pass
            p.standards_file_edit.textChanged.connect(p._update_load_both_enabled)
        except Exception as e:
            logging.warning(f"InputPanelBuilder: load_both enable wiring: {e}")

        _full_row(p.files_group)

        # LIMS controls (hidden unless Laser + LIMS)
        p._ensure_lims_controls()
        _full_row(p.lims_based_widget)
        p.dsn_file_edit = p.dsn_edit
        p.run_no_edit   = p.run_id_edit

        # --- 4) Laser "Isotopes to Process"
        p._ensure_isotope_controls()
        p.isotope_group.setVisible(False)
        _full_row(p.isotope_group)

        # --- 5a) Post-Processing — Laser
        p._ensure_laser_option_controls()
        p.pp_laser_group = QGroupBox("Post-Processing — Laser", p.input_panel)
        pp_laser_layout = QVBoxLayout(p.pp_laser_group)
        pp_laser_layout.setContentsMargins(4, 8, 4, 2)
        pp_laser_layout.setSpacing(2)

        def _lrow(label, w):
            h = QHBoxLayout()
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(QLabel(label, p.pp_laser_group))
            h.addWidget(w, 1)
            pp_laser_layout.addLayout(h)

        _lrow("Linearity:", p.linearity_combo)
        _lrow("Memory:",    p.memory_combo)
        _lrow("Drift:",     p.drift_combo)
        _lrow("MWL:",       p.mwl_combo)

        outlier_row = QHBoxLayout()
        outlier_row.setContentsMargins(0, 0, 0, 0)
        outlier_row.addWidget(QLabel("Outlier:", p.pp_laser_group))
        outlier_row.addWidget(p.outlier_method_combo, 1)
        pp_laser_layout.addLayout(outlier_row)

        factor_row = QHBoxLayout()
        factor_row.setContentsMargins(0, 0, 0, 0)
        factor_row.addWidget(QLabel("Factor:", p.pp_laser_group))
        factor_row.addWidget(p.outlier_factor_edit)
        pp_laser_layout.addLayout(factor_row)

        if not hasattr(p, "include_ignored_check"):
            p.include_ignored_check = QCheckBox("Include ignored injections", p.pp_laser_group)
            p.include_ignored_check.setToolTip(
                "If checked, rows with ignore ≠ 0 will be included if they contain δ-values."
            )
            p.include_ignored_check.setChecked(False)
        else:
            p.include_ignored_check.setParent(p.pp_laser_group)
        pp_laser_layout.addWidget(p.include_ignored_check)

        _full_row(p.pp_laser_group)
        p.pp_laser_group.setVisible(False)

        # --- 5b) Post-Processing — IRMS
        p._ensure_ea_option_controls()
        p.pp_irms_group = QGroupBox("Post-Processing — IRMS", p.input_panel)
        pp_irms_layout = QVBoxLayout(p.pp_irms_group)

        def _ea_row(label, w):
            h = QHBoxLayout()
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(QLabel(label, p.pp_irms_group))
            h.addWidget(w, 1)
            pp_irms_layout.addLayout(h)

        _ea_row("EA Linearity:", p.ea_linearity_combo)
        _ea_row("EA Drift:",     p.ea_drift_combo)

        p.ea_robust_check = QCheckBox("Robust fits (Huber)", p.pp_irms_group)
        robust_row = QHBoxLayout()
        robust_row.setContentsMargins(0, 0, 0, 0)
        robust_row.addWidget(QLabel("Robust:", p.pp_irms_group))
        robust_row.addWidget(p.ea_robust_check, 1)
        pp_irms_layout.addLayout(robust_row)
        p.ea_robust_check.setChecked(False)

        _full_row(p.pp_irms_group)
        p.pp_irms_group.setVisible(False)

        # --- 5c) EA Peak Selection
        p.ea_peak_group = QGroupBox("EA Peak Selection", p.input_panel)
        p.ea_peak_layout = QGridLayout(p.ea_peak_group)
        p.ea_peak_layout.setContentsMargins(4, 8, 4, 2)
        p.ea_peak_layout.setHorizontalSpacing(6)
        p.ea_peak_layout.setVerticalSpacing(2)
        p.ea_peak_layout.setColumnStretch(0, 0)
        p.ea_peak_layout.setColumnStretch(1, 1)
        _full_row(p.ea_peak_group)
        p.ea_peak_group.hide()

        # Clean up legacy EA peak combo attributes
        for old in ("ea_peak_c_combo", "ea_peak_n_combo"):
            if hasattr(p, old):
                try:
                    delattr(p, old)
                except Exception as e:
                    logging.warning(f"InputPanelBuilder: delattr {old}: {e}")

        # --- 6) Protocol Management
        protocol_group = QGroupBox("Protocol Management", p.input_panel)
        protocol_layout = QVBoxLayout(protocol_group)
        protocol_layout.setContentsMargins(4, 8, 4, 2)
        protocol_layout.setSpacing(2)

        if not hasattr(p, "btnManageProtocols"):
            p.btnManageProtocols = QPushButton("Manage Protocols...", protocol_group)
            p.btnManageProtocols.setIcon(IconCache.get_icon("settings"))
            p.btnManageProtocols.clicked.connect(p._open_protocol_manager)
        protocol_layout.addWidget(p.btnManageProtocols)

        if not hasattr(p, "btnCreateProtocol"):
            p.btnCreateProtocol = QPushButton("Save as Protocol...", protocol_group)
            p.btnCreateProtocol.setIcon(IconCache.get_icon("save"))
            p.btnCreateProtocol.clicked.connect(p._create_new_protocol)
        protocol_layout.addWidget(p.btnCreateProtocol)

        if not hasattr(p, "lblCurrentProtocol"):
            p.lblCurrentProtocol = QLabel("No protocol", p.input_panel)
            p.lblCurrentProtocol.setStyleSheet(
                "padding: 2px 4px; background: #f5f5f5; border-radius: 3px; font-size: 10px;"
            )
        protocol_layout.addWidget(p.lblCurrentProtocol)

        protocol_layout.addStretch(1)
        _full_row(protocol_group)

        # --- 7) Process + Save + Status  (sticky footer)
        process_layout = QVBoxLayout()
        process_layout.setSpacing(2)
        process_layout.setContentsMargins(0, 0, 0, 0)

        if not hasattr(p, "process_button"):
            p.process_button = QPushButton("Process Data", p.input_panel)
            p.process_button.setObjectName("processButton")
            p.process_button.setStyleSheet("""
                QPushButton {
                    background-color: #ef6c00;
                    color: #fff;
                    font-weight: 600;
                    padding: 3px 8px;
                    border: none;
                    border-radius: 5px;
                    max-height: 28px;
                }
                QPushButton:hover:!disabled { background-color: #e65100; }
                QPushButton:disabled {
                    background-color: #9e9e9e;
                    color: #f0f0f0;
                }
            """)
            p.process_button.clicked.connect(p.start_processing)
        p.process_button.setEnabled(False)
        process_layout.addWidget(p.process_button)

        if not hasattr(p, "save_db_btn"):
            p.save_db_btn = QPushButton("Save to Database", p.input_panel)
            p.save_db_btn.setObjectName("saveDbBtn")
            p.save_db_btn.setEnabled(False)
            p.save_db_btn.setStyleSheet("""
                QPushButton#saveDbBtn {
                    border-radius: 5px;
                    padding: 3px 8px;
                    font-weight: 600;
                    max-height: 26px;
                }
                QPushButton#saveDbBtn:enabled {
                    background-color: #2e7d32;
                    color: #ffffff;
                    border: 1px solid #1b5e20;
                }
                QPushButton#saveDbBtn:enabled:hover   { background-color: #388e3c; }
                QPushButton#saveDbBtn:enabled:pressed  { background-color: #2e7d32; }
                QPushButton#saveDbBtn:disabled {
                    background-color: #bdbdbd;
                    color: #eeeeee;
                    border: 1px solid #9e9e9e;
                }
            """)
            p.save_db_btn.setToolTip("Update SIAnalysisRun and upload data/results/fits")
            p.save_db_btn.clicked.connect(p._save_results_to_db_clicked)
        process_layout.addWidget(p.save_db_btn)

        if not hasattr(p, "status_label"):
            p.status_label = QLabel("Status: Ready", p.input_panel)
            p.status_label.setObjectName("StatusLabel")
            p.status_label.setAlignment(Qt.AlignCenter)
        process_layout.addWidget(p.status_label)

        _process_widget = QWidget()
        _process_widget.setLayout(process_layout)
        p._footer_layout.addWidget(_process_widget)

        # --- 8) Actions (sticky footer)
        if not hasattr(p, "action_export_csv"):
            p.action_export_csv = QAction(IconCache.get_icon("download"), "Export Results (CSV)...", p)
            p.action_export_csv.triggered.connect(p.export_results_unified)
            p.action_export_csv.setEnabled(False)

        if not hasattr(p, "action_export_templates"):
            p.action_export_templates = QAction(
                IconCache.get_icon("file-text"), "Download Standards Templates...", p
            )
            p.action_export_templates.triggered.connect(p.export_standards_templates)
            p.action_export_templates.setEnabled(True)

        actions_group = QGroupBox("Actions", p.input_panel)
        actions_layout = QVBoxLayout(actions_group)
        actions_layout.setContentsMargins(4, 8, 4, 2)
        actions_layout.setSpacing(2)

        if not hasattr(p, "btn_reset_app"):
            p.btn_reset_app = QPushButton("Start Over (Reset)", actions_group)
            p.btn_reset_app.setIcon(IconCache.get_icon("refresh-cw"))
            p.btn_reset_app.clicked.connect(
                lambda: p.reset_everything(keep_paths=True, keep_lims_inputs=True)
            )

        if not hasattr(p, "btn_actions_popup"):
            p.btn_actions_popup = QPushButton("Other Actions...", actions_group)
            p.btn_actions_popup.setIcon(IconCache.get_icon("menu"))
            p.btn_actions_popup.clicked.connect(p.on_actions_menu_requested)

        actions_layout.addWidget(p.btn_reset_app)
        actions_layout.addWidget(p.btn_actions_popup)
        p._footer_layout.addWidget(actions_group)

        # Final refresh
        p._rebuild_option_items()
        p._refresh_input_panel()

    # ------------------------------------------------------------------ #
    # File-format / protocol helpers                                       #
    # ------------------------------------------------------------------ #

    def _populate_file_formats(self):
        p = self.parent
        try:
            with QSignalBlocker(p.cmbFileFormat):
                p.cmbFileFormat.clear()
                formats = ProtocolManager.get_file_formats_for_module('SIAM')
                if not formats:
                    p.cmbFileFormat.addItem("(No formats available)", None)
                    p.cmbFileFormat.setEnabled(False)
                    logging.warning("InputPanelBuilder: no SIAM file formats found")
                    return
                for fmt in formats:
                    display = fmt['name']
                    if fmt['instrument_name']:
                        display += f" ({fmt['instrument_name']}"
                        if fmt['instrument_model']:
                            display += f" {fmt['instrument_model']}"
                        display += ")"
                    p.cmbFileFormat.addItem(display, fmt['id'])
                p.cmbFileFormat.setEnabled(True)
                logging.info(f"InputPanelBuilder: loaded {len(formats)} SIAM file formats")
        except Exception as e:
            logging.error(f"InputPanelBuilder._populate_file_formats: {e}", exc_info=True)
            p.cmbFileFormat.addItem("(Database error)", None)
            p.cmbFileFormat.setEnabled(False)

    def _on_format_changed(self):
        p = self.parent
        format_id = p.cmbFileFormat.currentData()
        if not format_id:
            return
        try:
            format_info = ProtocolManager.get_format_info(format_id)
            if not format_info:
                logging.warning(f"InputPanelBuilder: no info for format {format_id}")
                return
            instrument_name = format_info['instrument_name']
            logging.info(f"InputPanelBuilder: format {format_info['format_name']} → {instrument_name}")
            if instrument_name and hasattr(p, 'instrument_combo'):
                idx = p.instrument_combo.findText(instrument_name)
                if idx >= 0:
                    with QSignalBlocker(p.instrument_combo):
                        p.instrument_combo.setCurrentIndex(idx)
                        p.on_instrument_changed(instrument_name, format_id)
                else:
                    logging.warning(f"InputPanelBuilder: instrument '{instrument_name}' not in combo")
            self._load_default_protocol_for_format(format_id)
        except Exception as e:
            logging.error(f"InputPanelBuilder._on_format_changed: {e}", exc_info=True)

    def _load_default_protocol_for_format(self, file_format_id: int):
        p = self.parent
        try:
            protocol = ProtocolManager.get_default_protocol('SIAM', file_format_id)
            if protocol:
                p.current_protocol = protocol
                self._apply_protocol_settings(protocol)
                if hasattr(p, 'lblCurrentProtocol'):
                    p.lblCurrentProtocol.setText(f"Protocol: {protocol.name}")
                    p.lblCurrentProtocol.setStyleSheet(
                        "padding: 4px 8px; background: #e8f5e9; color: #2e7d32; "
                        "font-weight: bold; border-radius: 3px;"
                    )
                logging.info(f"InputPanelBuilder: loaded protocol '{protocol.name}'")
            else:
                p.current_protocol = None
                if hasattr(p, 'lblCurrentProtocol'):
                    p.lblCurrentProtocol.setText("No default protocol")
                    p.lblCurrentProtocol.setStyleSheet(
                        "padding: 4px 8px; background: #fff3e0; color: #e65100; border-radius: 3px;"
                    )
                logging.info(f"InputPanelBuilder: no default protocol for format {file_format_id}")
        except Exception as e:
            logging.error(f"InputPanelBuilder._load_default_protocol_for_format: {e}", exc_info=True)

    def _apply_protocol_settings(self, protocol: Protocol):
        p = self.parent
        if not protocol or not protocol.settings:
            logging.warning("InputPanelBuilder._apply_protocol_settings: no settings")
            return

        if hasattr(p, '_ensure_laser_option_controls'):
            p._ensure_laser_option_controls()
        if hasattr(p, '_ensure_ea_option_controls'):
            p._ensure_ea_option_controls()

        settings = protocol.settings

        SETTING_WIDGETS = {
            'drift_correction':    ['drift_combo', 'ea_drift_combo'],
            'linearity_correction': ['linearity_combo', 'ea_linearity_combo'],
            'memory_correction':   ['memory_combo'],
        }

        for setting_key, widget_names in SETTING_WIDGETS.items():
            if setting_key in settings:
                for widget_name in widget_names:
                    if hasattr(p, widget_name):
                        self._set_combo_safe(widget_name, settings[setting_key])
                        break

        if 'mwl' in settings and hasattr(p, 'mwl_combo'):
            self._set_combo_safe('mwl_combo', settings['mwl'])

        if 'min_injections' in settings and hasattr(p, 'spnMinInjections'):
            p.spnMinInjections.setValue(settings['min_injections'])
        if 'water_conc_min' in settings and hasattr(p, 'spnWaterMin'):
            p.spnWaterMin.setValue(settings['water_conc_min'])
        if 'water_conc_max' in settings and hasattr(p, 'spnWaterMax'):
            p.spnWaterMax.setValue(settings['water_conc_max'])
        if 'outlier_sigma' in settings and hasattr(p, 'spnOutlierSigma'):
            p.spnOutlierSigma.setValue(settings['outlier_sigma'])

        logging.info(f"InputPanelBuilder: applied settings from '{protocol.name}'")

    def _build_protocol_from_current_settings(self) -> Protocol:
        p = self.parent
        import datetime as dt_mod

        format_id = p.cmbFileFormat.currentData() if hasattr(p, 'cmbFileFormat') else None
        if not format_id:
            raise ValueError("No file format selected — cannot create protocol")

        def _combo_val(widget_names: list, default: str = 'None') -> str:
            for name in widget_names:
                if hasattr(p, name):
                    return getattr(p, name).currentText()
            return default

        settings = {
            'drift_correction':    _combo_val(['drift_combo', 'ea_drift_combo'], 'Linear'),
            'linearity_correction': _combo_val(['linearity_combo', 'ea_linearity_combo'], 'None'),
            'memory_correction':   _combo_val(['memory_combo'], 'None'),
        }

        mwl = _combo_val(['mwl_combo'], None)
        if mwl and mwl != 'None':
            settings['mwl'] = mwl

        for attr, key in [
            ('spnMinInjections', 'min_injections'),
            ('spnWaterMin',      'water_conc_min'),
            ('spnWaterMax',      'water_conc_max'),
            ('spnOutlierSigma',  'outlier_sigma'),
        ]:
            if hasattr(p, attr):
                settings[key] = getattr(p, attr).value()

        return Protocol(
            id=-1,
            name=f"SIAM {dt_mod.datetime.now().strftime('%Y%m%d_%H%M')}",
            module='SIAM',
            format_id=format_id,
            description="From current settings",
            settings=settings,
            is_active=True,
            is_default=False,
        )

    # ------------------------------------------------------------------ #
    # Utilities                                                            #
    # ------------------------------------------------------------------ #

    def _set_combo_safe(self, widget_name: str, value: str):
        p = self.parent
        if not hasattr(p, widget_name):
            return
        combo = getattr(p, widget_name)
        idx = combo.findText(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)
            logging.debug(f"InputPanelBuilder: set {widget_name} = {value!r}")
        else:
            logging.warning(f"InputPanelBuilder: {value!r} not found in {widget_name}")

    def _set_spinbox_safe(self, widget_name: str, value):
        p = self.parent
        if not hasattr(p, widget_name):
            return
        try:
            if isinstance(value, (int, float)):
                getattr(p, widget_name).setValue(value)
                logging.debug(f"InputPanelBuilder: set {widget_name} = {value}")
        except Exception as e:
            logging.warning(f"InputPanelBuilder._set_spinbox_safe({widget_name}): {e}")

    def _update_protocol_display(self, protocol=None):
        p = self.parent
        if not hasattr(p, 'lblCurrentProtocol'):
            logging.debug("InputPanelBuilder: lblCurrentProtocol not found")
            return
        if protocol:
            instrument = protocol.settings.get('instrument_name', '')
            display_text = f"Protocol: {protocol.name}"
            if instrument:
                display_text += f" ({instrument})"
            p.lblCurrentProtocol.setText(display_text)
            p.lblCurrentProtocol.setStyleSheet(
                "font-weight: bold; color: #2e7d32; background-color: #e8f5e9; "
                "padding: 4px 8px; border-radius: 3px;"
            )
            tooltip = f"Protocol: {protocol.name}\nModule: {protocol.module}"
            if instrument:
                tooltip += f"\nInstrument: {instrument}"
            if protocol.description:
                tooltip += f"\n\n{protocol.description}"
            p.lblCurrentProtocol.setToolTip(tooltip)
        else:
            p.lblCurrentProtocol.setText("No protocol loaded")
            p.lblCurrentProtocol.setStyleSheet(
                "font-weight: bold; color: #757575; background-color: #f5f5f5; "
                "padding: 4px 8px; border-radius: 3px;"
            )
            p.lblCurrentProtocol.setToolTip("No protocol currently loaded")
