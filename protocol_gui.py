"""
Unified Protocol GUI (v2.1)
===========================
Changes from v2.0
-----------------
* FIX  – ProtocolGUI.__init__ detects LSC vs SIAM from positional-arg type,
          resolving TypeError when called as ProtocolManagerDialog(parent, int, int).
* FIX  – SIAM SIAMSettingsWidget: removed chkOutlier enable checkbox;
          replaced with single cmbOutlier combo whose first item is "None"
          (populated from shared outliers.py constants).
* FIX  – LSC LSCSettingsWidget: same pattern – cmbOutlier with "None" first.
* NEW  – Both widgets import method lists from outliers.py (single source of truth).
* FIX  – Column mapping table title is "Column Mappings  (File → TRIMS)".
"""
from __future__ import annotations

import sys
import logging
from typing import Optional

from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QComboBox, QLineEdit, QTextEdit, QPushButton, QStackedWidget,
    QGroupBox, QFormLayout, QCheckBox, QDoubleSpinBox, QSpinBox, QMessageBox,
    QSplitter, QWidget, QTableWidget, QTableWidgetItem, QHeaderView, QFrame,
    QScrollArea,QTabWidget
)
from PyQt5.QtCore import Qt

from protocol_manager import ProtocolManager, Protocol, ColumnMapping
from outliers import OUTLIER_METHODS, OUTLIER_TOOLTIP
from siam_column_resolver import SIAM_TARGET_FIELDS, COLUMN_ALIASES, InstrumentType

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

class _BlockSignals:
    def __init__(self, w):  self._w = w
    def __enter__(self):    self._w.blockSignals(True)
    def __exit__(self, *_): self._w.blockSignals(False)

# ──────────────────────────────────────────────────────────────────────────────
# BASE
# ──────────────────────────────────────────────────────────────────────────────

class BaseSettingsWidget(QWidget):
    def get_data(self) -> dict: return {}
    def set_data(self, data: dict): pass
    def get_mandatory_columns(self) -> list: return []

# ──────────────────────────────────────────────────────────────────────────────
# LSC SETTINGS
# ──────────────────────────────────────────────────────────────────────────────

class LSCSettingsWidget(BaseSettingsWidget):
    """LSC / Tritium configuration."""

    def __init__(self):
        super().__init__()
        f = QFormLayout(self)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.cmbMetric = QComboBox()
        self.cmbMetric.addItems(["CPM", "DPM"])

        self.cmbEff = QComboBox()
        self.cmbEff.addItems(["Per-cycle (file)", "Per-run (computed)", "Per-run (file)"])

        self.cmbBg = QComboBox()
        self.cmbBg.addItems(["UseFile", "Manual", "Calculated"])

        # Outlier – "None" first, no separate Enable checkbox
        self.cmbOutlier = QComboBox()
        self.cmbOutlier.addItems(OUTLIER_METHODS)
        self.cmbOutlier.setToolTip(OUTLIER_TOOLTIP)

        self.spnSigma = QDoubleSpinBox()
        self.spnSigma.setRange(0.1, 20.0); self.spnSigma.setValue(2.0)
        self.spnSigma.setToolTip("σ multiplier / max-N for GESD")

        self.cmbActivityUnit = QComboBox()
        self.cmbEFMethod = QComboBox()
        
        f.addRow("Signal Metric:",         self.cmbMetric)
        f.addRow("Efficiency Source:",     self.cmbEff)
        f.addRow("Background Mode:",       self.cmbBg)
        f.addRow("Outlier Method:",        self.cmbOutlier)
        f.addRow("Outlier Threshold (σ):", self.spnSigma)
        f.addRow('EF Method:', self.cmbEFMethod)
        f.addRow('Activity Unit:', self.cmbActivityUnit)
        
        self._populate_calc_combos()
        
    def get_data(self) -> dict:
        return {
            "signal_metric":            self.cmbMetric.currentText(),
            "efficiency_source":        self.cmbEff.currentText(),
            "background_mode":          self.cmbBg.currentText(),
            "outlier_method":           self.cmbOutlier.currentText(),
            "outlier_threshold":        self.spnSigma.value(),
            "activity_unit":            self.cmbActivityUnit.currentData(),
            "enrichment_factor_method": self.cmbEFMethod.currentData(),
        }

    def set_data(self, settings: dict):
        """Apply settings to widgets. Accepts both old key names and canonical snake_case names."""
        def _set_combo_text(combo, val):
            if val is None:
                return
            idx = combo.findText(str(val))
            if idx >= 0:
                combo.setCurrentIndex(idx)

        def _set_combo_data(combo, val):
            if val is None:
                return
            # Try as int first (canonical storage), fall back to text search
            try:
                idx = combo.findData(int(val))
            except (TypeError, ValueError):
                idx = -1
            if idx < 0:
                idx = combo.findText(str(val))
            if idx >= 0:
                combo.setCurrentIndex(idx)

        _set_combo_text(self.cmbMetric,  settings.get('signal_metric') or settings.get('metric'))
        _set_combo_text(self.cmbEff,     settings.get('efficiency_source') or settings.get('efficiency_mode'))
        _set_combo_text(self.cmbBg,      settings.get('background_mode'))
        _set_combo_text(self.cmbOutlier, settings.get('outlier_method'))

        sigma = settings.get('outlier_threshold') or settings.get('outlier_sigma')
        if sigma is not None:
            self.spnSigma.setValue(float(sigma))

        _set_combo_data(self.cmbActivityUnit,       settings.get('activity_unit'))
        _set_combo_data(self.cmbEFMethod,           settings.get('enrichment_factor_method'))

    def _populate_calc_combos(self):
        """Populate Unit and Method selections from database"""
        try:
            from db_core import db_manager
            from sqlalchemy import text
            
            with db_manager.get_connection() as conn:
                # Activity Units
                units = conn.execute(text(
                    "SELECT UnitID, ShortName FROM MeasurementUnit "
                    "WHERE UnitID < 5 ORDER BY UnitID"
                )).fetchall()
                
                for u in units:
                    self.cmbActivityUnit.addItem(u.ShortName, u.UnitID)
                
                # Default to TU (UnitID=1)
                self.cmbActivityUnit.setCurrentIndex(
                    self.cmbActivityUnit.findData(1)
                )
                
                # EF Methods
                _sep = "' --- '"
                _concat = db_manager.sql_concat('ID', _sep, 'sName')
                methods = conn.execute(text(
                    f"SELECT ID, {_concat} "
                    "FROM GuitblEnrichmentFactorMethod "
                    "WHERE ID IN (0,1,2,4) ORDER BY ID"
                )).fetchall()
                
                for m in methods:
                    self.cmbEFMethod.addItem(m[1], m[0])
                
                # Default to method 2
                self.cmbEFMethod.setCurrentIndex(
                    self.cmbEFMethod.findData(2)
                )
        
        except Exception as e:
            import logging
            logging.error(f"Failed to populate calc combos: {e}", exc_info=True)
            # Add fallback defaults
            if self.cmbActivityUnit.count() == 0:
                self.cmbActivityUnit.addItems(['TU', 'Bq', 'pMC', 'dpm/mL'])
            if self.cmbEFMethod.count() == 0:
                self.cmbEFMethod.addItems(['0 --- Method0', '1 --- Method1', '2 --- Method2'])
    
    def get_mandatory_columns(self):
        """Get mandatory columns for LSC/TRIMS."""
        return [
            ("CPM", "Counts per minute (use Is Net to specify net/gross)"),
            ("DPM", "Disintegrations per minute"),
            ("Efficiency", "Counting efficiency (%)"),
            ("Background", "Background counts"),
            ("QIP", "Quality indicator parameter (optional)"),
            ("Sample", "Sample identifier"),
            ("DateTime", "Date/time of measurement"),
            ("Cycle", "Cycle number"),
        ]
    
# ──────────────────────────────────────────────────────────────────────────────
# SIAM SETTINGS
# ──────────────────────────────────────────────────────────────────────────────

_ISO_LASER   = ["d18O", "dD", "d17O"]
_ISO_IRMS_EA = ["d13C", "d15N", "d34S"]
_ISO_IRMS_DI = ["d13C", "d18O", "dD", "d34S"]


_WATER_DEFAULTS = {
    "LGR":     (2000,  25000),
    "Picarro": (5000,  25000),
}

class SIAMSettingsWidget(BaseSettingsWidget):
    """SIAM / Stable Isotope configuration — mirrors SettingsDialog exactly."""

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        self._vbox = QVBoxLayout(body); self._vbox.setSpacing(6)
        scroll.setWidget(body); outer.addWidget(scroll)

        self._build_instrument()
        self._build_isotopes()
        self._build_pipeline()
        self._build_laser_group()
        self._build_irms_group()
        self._vbox.addStretch()

        self.cmbInstrument.currentTextChanged.connect(self._on_instrument_changed)
        self._on_instrument_changed(self.cmbInstrument.currentText())

    # ── builders ─────────────────────────────────────────────────────────────

    def _build_instrument(self):
        g = QGroupBox("Instrument"); f = QFormLayout(g)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.cmbInstrument = QComboBox()
        self.cmbInstrument.addItems(["LGR", "Picarro", "IRMS (EA)", "IRMS (Thermo DI)"])
        self.cmbCalibScale = QComboBox()
        self.cmbCalibScale.addItems(["VSMOW", "VPDB", "AIR", "CDT"])
        f.addRow("Instrument:",         self.cmbInstrument)
        f.addRow("Calibration Scale:",  self.cmbCalibScale)
        self._vbox.addWidget(g)

    def _build_isotopes(self):
        self.grpIsotope = QGroupBox("Primary Isotopes  (select all that apply)")
        v = QVBoxLayout(self.grpIsotope)

        self.grpIsoLaser = QGroupBox("Laser  [LGR / Picarro]")
        hl = QHBoxLayout(self.grpIsoLaser); self._chk_laser = {}
        for iso in _ISO_LASER:
            c = QCheckBox(iso); c.setChecked(iso in ("d18O", "dD"))
            self._chk_laseriso = c; hl.addWidget(c)
        hl.addStretch()

        self.grpIsoEA = QGroupBox("IRMS  (EA)")
        he = QHBoxLayout(self.grpIsoEA); self._chk_ea = {}
        for iso in _ISO_IRMS_EA:
            c = QCheckBox(iso); c.setChecked(iso in ("d13C", "d15N"))
            self._chk_eaiso = c; he.addWidget(c)
        he.addStretch()

        self.grpIsoDI = QGroupBox("IRMS  (Thermo DI)")
        hd = QHBoxLayout(self.grpIsoDI); self._chk_di = {}
        for iso in _ISO_IRMS_DI:
            c = QCheckBox(iso); c.setChecked(iso in ("d18O", "dD"))
            self._chk_diiso = c; hd.addWidget(c)
        hd.addStretch()

        v.addWidget(self.grpIsoLaser)
        v.addWidget(self.grpIsoEA)
        v.addWidget(self.grpIsoDI)
        self._vbox.addWidget(self.grpIsotope)

    def _build_pipeline(self):
        """Combos match SettingsDialog in siam_processor_gui.py exactly."""
        g = QGroupBox("Correction Pipeline"); f = QFormLayout(g)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        # Memory – SettingsDialog.cmb_memory
        self.cmbMemory = QComboBox()
        self.cmbMemory.addItems([
            "1-Reservoir (Single Pool)",
            "Fast/Slow Pool Carryover",
            "Exponential",
            "Asymptotic",
            "None",
        ])
        self.cmbMemory.setCurrentText("1-Reservoir (Single Pool)")

        # Drift – SettingsDialog.cmb_drift
        self.cmbDrift = QComboBox()
        self.cmbDrift.addItems(["Linear", "None"])

        # Linearity – SettingsDialog.cmb_linearity
        self.cmbLinearity = QComboBox()
        self.cmbLinearity.addItems(["None", "Linear"])

        # Calibration
        self.cmbCalib = QComboBox()
        self.cmbCalib.addItems(["TwoPoint", "MultiPoint", "SinglePoint"])

        # MWL – SettingsDialog.cmb_mwl (Laser only)
        self.cmbMWL = QComboBox()
        self.cmbMWL.addItems(["GMWL_Craig", "GMWL_Gat", "GardMWL", "HerMWL", "MMWL", "None"])
        self._lblMWL = QLabel("MWL:")

        f.addRow("Linearity Correction:", self.cmbLinearity)
        f.addRow("Memory Correction:",    self.cmbMemory)
        f.addRow("Drift Correction:",     self.cmbDrift)        
        f.addRow("Calibration:",          self.cmbCalib)
        f.addRow(self._lblMWL,            self.cmbMWL)
        self._vbox.addWidget(g)

    def _build_laser_group(self):
        self.grpLaser = QGroupBox("Laser Settings"); f = QFormLayout(self.grpLaser)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.spnMinInj   = QSpinBox();         self.spnMinInj.setRange(1, 20); self.spnMinInj.setValue(5)
        self.spnWaterMin = QDoubleSpinBox();   self.spnWaterMin.setRange(0, 99999); self.spnWaterMin.setSuffix(" ppm")
        self.spnWaterMax = QDoubleSpinBox();   self.spnWaterMax.setRange(0, 99999); self.spnWaterMax.setSuffix(" ppm")
        
        # Include Ignored Injections checkbox
        self.chkIncludeIgnored = QCheckBox("Include ignored injections in processing")
        self.chkIncludeIgnored.setChecked(False)

        # Outlier – "None" first, from outliers.py SIAM list, no chkEnable
        self.cmbOutlier = QComboBox()
        self.cmbOutlier.addItems(OUTLIER_METHODS)
        self.cmbOutlier.setToolTip(OUTLIER_TOOLTIP)

        self.spnOutlierSD = QDoubleSpinBox(); self.spnOutlierSD.setRange(0.1, 10.0); self.spnOutlierSD.setValue(2.0)
        self.spnZWarn     = QDoubleSpinBox(); self.spnZWarn.setRange(0.1, 10.0);     self.spnZWarn.setValue(2.0)
        self.spnZFail     = QDoubleSpinBox(); self.spnZFail.setRange(0.1, 10.0);     self.spnZFail.setValue(3.0)

        f.addRow("Min Injections:",    self.spnMinInj)
        f.addRow("H₂O Conc Min:",      self.spnWaterMin)
        f.addRow("H₂O Conc Max:",      self.spnWaterMax)
        f.addRow(self.chkIncludeIgnored)

        _sep = QLabel("── Outlier Detection ──"); _sep.setStyleSheet("color:gray;font-size:10px;")
        f.addRow(_sep)
        f.addRow("Outlier Method:",    self.cmbOutlier)
        f.addRow("Outlier SD Factor:", self.spnOutlierSD)
        f.addRow("z warn:",            self.spnZWarn)
        f.addRow("z fail:",            self.spnZFail)

        self._vbox.addWidget(self.grpLaser)
        self._apply_water_defaults("LGR")

    def _build_irms_group(self):
        self.grpIRMS = QGroupBox("IRMS / EA Settings"); f = QFormLayout(self.grpIRMS)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.chkRobust = QCheckBox("Robust fits (Huber)")
        f.addRow(self.chkRobust)

        all_irms = ["d13C", "d15N", "d34S", "d18O", "dD"]
        _s1 = QLabel("── Blank Thresholds (‰) ──"); _s1.setStyleSheet("color:gray;font-size:10px;")
        f.addRow(_s1)
        self._ed_blank = {}
        for iso in all_irms:
            spn = QDoubleSpinBox(); spn.setRange(0, 100); spn.setDecimals(3); spn.setSuffix(" ‰")
            self._ed_blankiso = spn; f.addRow(f"Blank threshold ({iso}):", spn)

        _s2 = QLabel("── Preferred Peak # ──"); _s2.setStyleSheet("color:gray;font-size:10px;")
        f.addRow(_s2)
        self._sp_peaks = {}
        for iso in all_irms:
            sp = QSpinBox(); sp.setRange(1, 20); sp.setValue(1)
            self._sp_peaksiso = sp; f.addRow(f"Preferred peak ({iso}):", sp)

        self._vbox.addWidget(self.grpIRMS)

    # ── visibility ────────────────────────────────────────────────────────────

    def _on_instrument_changed(self, text: str):
        is_laser = text in ("LGR", "Picarro")
        is_ea    = text == "IRMS (EA)"
        is_di    = text == "IRMS (Thermo DI)"
        self.grpIsoLaser.setVisible(is_laser)
        self.grpIsoEA.setVisible(is_ea)
        self.grpIsoDI.setVisible(is_di)
        self.grpLaser.setVisible(is_laser)
        self.grpIRMS.setVisible(is_ea or is_di)
        self._lblMWL.setVisible(is_laser)
        self.cmbMWL.setVisible(is_laser)
        # self.cmbLaserSub.currentTextChanged.connect(self._apply_water_defaults)
        if is_laser:
            with _BlockSignals(self.cmbInstrument):
                self.cmbInstrument.setCurrentText(text)
            self._apply_water_defaults(text)

    def _apply_water_defaults(self, laser_instr: str):
        lo, hi = _WATER_DEFAULTS.get(laser_instr, (5000, 25000))
        self.spnWaterMin.setValue(lo)
        self.spnWaterMax.setValue(hi)

    # ── data ─────────────────────────────────────────────────────────────────

    def _selected_isotopes(self) -> list:
        instr = self.cmbInstrument.currentText()
        if instr in ("LGR", "Picarro"):
            return [k for k, c in self._chk_laser.items() if c.isChecked()]
        elif instr == "IRMS (EA)":
            return [k for k, c in self._chk_ea.items() if c.isChecked()]
        else:
            return [k for k, c in self._chk_di.items() if c.isChecked()]

    def get_data(self) -> dict:
        instr    = self.cmbInstrument.currentText()
        is_laser = instr in ("LGR", "Picarro")

        d = {
            "instrument_name":             instr,
            "calibration_scale":           self.cmbCalibScale.currentText(),
            "primary_isotopes":            self._selected_isotopes(),
            "linearity_correction":        self.cmbLinearity.currentText(),
            "memory_correction":           self.cmbMemory.currentText(),
            "drift_correction":            self.cmbDrift.currentText(),
            
            "calibration_method":          self.cmbCalib.currentText(),
        }
        if is_laser:
            d.update({
                "mwl":               self.cmbMWL.currentText(),
                "min_injections":    self.spnMinInj.value(),
                "water_conc_min":    self.spnWaterMin.value(),
                "water_conc_max":    self.spnWaterMax.value(),
                "include_ignored":   self.chkIncludeIgnored.isChecked(),
                "outlier_method":    self.cmbOutlier.currentText(),  # "None" means disabled
                "outlier_sd_factor": self.spnOutlierSD.value(),
                "outlier_z_warn":    self.spnZWarn.value(),
                "outlier_z_fail":    self.spnZFail.value(),
            })
        else:
            d.update({
                "robust_fits":        self.chkRobust.isChecked(),
                "blank_thresholds":   {k: v.value() for k, v in self._ed_blank.items() if v.value() > 0},
                "ea_peak_preference": {k: v.value() for k, v in self._sp_peaks.items()},
            })
        return d

    def set_data(self, data: dict):
        instr = data.get("instrument_name", "LGR")
        self.cmbInstrument.setCurrentText(instr)   # triggers _on_instrument_changed
        self.cmbCalibScale.setCurrentText(data.get("calibration_scale",     "VSMOW"))
        self.cmbMemory.setCurrentText(data.get("memory_correction",         "1-Reservoir (Single Pool)"))
        self.cmbDrift.setCurrentText( data.get("drift_correction",          "Linear"))
        self.cmbLinearity.setCurrentText(data.get("linearity_correction",   "None"))
        self.cmbCalib.setCurrentText( data.get("calibration_method",        "TwoPoint"))

        selected = set(data.get("primary_isotopes", []))
        for iso, chk in self._chk_laser.items(): chk.setChecked(iso in selected)
        for iso, chk in self._chk_ea.items():    chk.setChecked(iso in selected)
        for iso, chk in self._chk_di.items():    chk.setChecked(iso in selected)

        if instr in ("LGR", "Picarro"):
            self.cmbMWL.setCurrentText(data.get("mwl", "GMWL_Craig"))
            self.spnMinInj.setValue(int(data.get("min_injections", 5)))
            lo, hi = _WATER_DEFAULTS.get(instr, (5000, 25000))
            self.spnWaterMin.setValue(float(data.get("water_conc_min", lo)))
            self.spnWaterMax.setValue(float(data.get("water_conc_max", hi)))
            self.chkIncludeIgnored.setChecked(bool(data.get("include_ignored", False)))
            self.cmbOutlier.setCurrentText(data.get("outlier_method",    "None"))
            self.spnOutlierSD.setValue(float(data.get("outlier_sd_factor", 2.0)))
            self.spnZWarn.setValue(    float(data.get("outlier_z_warn",    2.0)))
            self.spnZFail.setValue(    float(data.get("outlier_z_fail",    3.0)))
        else:
            self.chkRobust.setChecked(bool(data.get("robust_fits", False)))
            blanks = data.get("blank_thresholds", {})
            for iso, spn in self._ed_blank.items(): spn.setValue(float(blanks.get(iso, 0.0)))
            peaks = data.get("ea_peak_preference", {})
            for iso, sp in self._sp_peaks.items():   sp.setValue(int(peaks.get(iso, 1)))

    def get_mandatory_columns(self):
        """
        Get mandatory columns for current instrument.
        REUSES siam_column_resolver.SIAM_TARGET_FIELDS as single source of truth.
        """
        # Get current instrument
        instrument_label = self.cmbInstrument.currentText()
        
        # Convert to InstrumentType
        instrument_type = InstrumentType.from_label(instrument_label)
        if not instrument_type:
            instrument_type = InstrumentType.LASER  # Default fallback
        
        # Get target fields from resolver
        target_fields = SIAM_TARGET_FIELDS.get(instrument_type, [])
        
        # Add friendly descriptions
        desc = {
            "sample_id": "Sample identifier",
            "timestamp": "Date/time of measurement",
            "injection_no": "Injection number",
            "water_conc": "Water concentration (ppm)",
            "d18O": "δ18O value (‰)",
            "dD": "δD value (‰)",
            "d17O": "δ17O value (‰)",
            "d13C": "δ13C value (‰)",
            "d15N": "δ15N value (‰)",
            "d34S": "δ34S value (‰)",
            "Area": "Peak area",
            "block_no": "Block/run number",
            "Analysis": "LIMS Analysis ID",
        }
        
        return [(field, desc.get(field, field)) for field in target_fields]

# ──────────────────────────────────────────────────────────────────────────────
# MAIN DIALOG
# ──────────────────────────────────────────────────────────────────────────────

class ProtocolGUI(QDialog):
    """
    Unified Protocol Manager.

    Call signatures
    ---------------
    LSC   :  ProtocolGUI(parent, isotope_id, format_id)   ← positional ints
    SIAM  :  ProtocolGUI(parent, module='SIAM')           ← keyword str
    """

    def __init__(self, parent=None, _arg2=None, _arg3=None, *, 
                 module: Optional[str] = None, 
                 protocol: Optional[Protocol] = None,
                 current_protocol_id: Optional[int] = None,
                 file_headers: Optional[list[str]] = None,
                 restrict_to_module: bool = True):
        """
        Multi-mode constructor supporting:
        
        1. LSC: ProtocolGUI(parent, isotope_id, format_id)
           _arg2 = isotope_id (int), _arg3 = format_id (int)
        
        2. SIAM: ProtocolGUI(parent, module='SIAM')
           module = 'SIAM' (keyword)
        
        3. Editor: ProtocolGUI(parent, protocol=protocol_obj)
           protocol = Protocol object to pre-load
        """
        super().__init__(parent)
        
        self.current_id = -1
        self.selected_protocol = None
        self.protocol = None  # Stores saved protocol for access via dlg.protocol
        self._preload_protocol = protocol  # Store for loading after UI built
        self._edit_mode = False  # Track if in edit mode
        self._restrict_to_module = restrict_to_module
        self._file_headers = file_headers
        self._preload_protocol_id = current_protocol_id
        
        # ── Detect call mode ──────────────────────────────────────────────
        if isinstance(_arg2, int):
            # LSC: ProtocolManagerDialog(parent, isotope_id, format_id)
            self._ctx_module  = "LSC"
            self._ctx_isotope = _arg2
            self._ctx_format  = _arg3
        elif isinstance(_arg2, str):
            # module passed positionally (shouldn't happen but be safe)
            self._ctx_module  = _arg2
            self._ctx_isotope = None
            self._ctx_format  = None
        else:
            # SIAM / generic: ProtocolManagerDialog(parent, module='SIAM')
            # OR: ProtocolEditorDialog(parent, protocol=protocol_obj)
            self._ctx_module  = module if not protocol else protocol.module
            self._ctx_isotope = None
            self._ctx_format  = None

        if self._restrict_to_module and self._ctx_module:
            self.setWindowTitle(f"{self._ctx_module} Protocol Manager")
        else:
            self.setWindowTitle("Protocol Manager")
        self.resize(1120, 820)
        
        self._build_ui()
        self.refresh_list()
        
        # If protocol was provided, load it and enter edit mode
        if self._preload_protocol:
            self._load_protocol_from_object(self._preload_protocol)
            self._edit_mode = True
            self._update_button_states()
        elif self._preload_protocol_id:  # NEW!
            # Find and select the current protocol
            self._select_protocol_by_id(self._preload_protocol_id)
        elif self._file_headers and self._ctx_module in ("SIAM", "LSC") and self.tblMap.rowCount() == 0:
            self._populate_mandatory_columns()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QHBoxLayout(self)

        # ── left ──
        left = QWidget(); vl = QVBoxLayout(left)

        self.cmbFilter = QComboBox()
        self.cmbFilter.addItems(["All", "LSC", "SIAM", "NGAM"])
        if self._ctx_module:                                    # always a str now
            self.cmbFilter.setCurrentText(self._ctx_module)
        self.cmbFilter.currentTextChanged.connect(self.refresh_list)

        if self._restrict_to_module and self._ctx_module:
            self.cmbFilter.setCurrentText(self._ctx_module)
            self.cmbFilter.setEnabled(False)
    
        self.lstProtocols = QListWidget()
        self.lstProtocols.itemClicked.connect(self._load_protocol)
        self.lstProtocols.itemDoubleClicked.connect(self._select_and_close)
        self.lstProtocols.itemSelectionChanged.connect(self._update_button_states)

        brow = QHBoxLayout()
        self.btnNew = QPushButton("New")
        self.btnNew.setShortcut("Ctrl+N")
        self.btnNew.setToolTip("Create new protocol (Ctrl+N)")
        
        self.btnEdit = QPushButton("Edit")
        self.btnEdit.setShortcut("Ctrl+E")
        self.btnEdit.setToolTip("Edit selected protocol (Ctrl+E)")
        
        self.btnDuplicate = QPushButton("Duplicate")
        self.btnDuplicate.setShortcut("Ctrl+D")
        self.btnDuplicate.setToolTip("Duplicate selected protocol (Ctrl+D)")
        
        self.btnDelete = QPushButton("Delete")
        self.btnDelete.setShortcut("Delete")
        self.btnDelete.setToolTip("Delete selected protocol (Delete key)")

        self.btnNew.clicked.connect(self._new_protocol)
        self.btnEdit.clicked.connect(self._edit_protocol)
        self.btnDuplicate.clicked.connect(self._duplicate_protocol)
        self.btnDelete.clicked.connect(self._delete_protocol)
                
        brow.addWidget(self.btnNew)
        brow.addWidget(self.btnEdit)
        brow.addWidget(self.btnDuplicate)
        brow.addWidget(self.btnDelete)

        # Search box
        self.txtSearch = QLineEdit()
        self.txtSearch.setPlaceholderText("🔍 Search protocols...")
        self.txtSearch.setClearButtonEnabled(True)
        self.txtSearch.textChanged.connect(self._filter_protocols)
        
        vl.addWidget(QLabel("Module filter:"))
        vl.addWidget(self.cmbFilter)
        vl.addWidget(self.lstProtocols, stretch=1)
        vl.addLayout(brow)

        # ── right ──
        right = QWidget(); vr = QVBoxLayout(right)

        fhdr = QFormLayout()
        fhdr.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.txtName   = QLineEdit()
        self.cmbModule = QComboBox(); self.cmbModule.addItems(["LSC", "SIAM", "NGAM"])
        if self._ctx_module:
            self.cmbModule.setCurrentText(self._ctx_module)
            
        if self._restrict_to_module and self._ctx_module:
            self.cmbModule.setEnabled(False)
                        
        self.cmbModule.currentTextChanged.connect(self._on_module_changed)
        
        # File Format (populated based on module)
        self.cmbFileFormat = QComboBox()
        self.cmbFileFormat.setToolTip("File format from GUItblFileFormat table")
        
        # Flags
        self.chkActive = QCheckBox("Active")
        self.chkActive.setChecked(True)
        self.chkActive.setToolTip("Inactive protocols are hidden from selection")
        
        self.chkDefault = QCheckBox("Default")
        self.chkDefault.setToolTip("Default protocol for this configuration")
        
        self.txtDesc = QTextEdit(); self.txtDesc.setMaximumHeight(52)
        
        fhdr.addRow("Protocol Name:", self.txtName)
        fhdr.addRow("Module:",        self.cmbModule)
        fhdr.addRow("File Format:",   self.cmbFileFormat)
        
        # Flags in horizontal layout
        flagsLayout = QHBoxLayout()
        flagsLayout.addWidget(self.chkActive)
        flagsLayout.addWidget(self.chkDefault)
        flagsLayout.addStretch()
        fhdr.addRow("Flags:", flagsLayout)
        
        fhdr.addRow("Description:",   self.txtDesc)
        vr.addLayout(fhdr)

        # ── TABS for Settings and Mappings ──────────────────────────────────
        self.tabs = QTabWidget()
        
        # Tab 1: Processing Settings
        tab_settings = QWidget()
        layout_settings = QVBoxLayout(tab_settings)
        layout_settings.setContentsMargins(0, 0, 0, 0)
        
        # Settings (stacked widget moved into tab)
        grpS = QGroupBox("Processing Settings")
        vsS = QVBoxLayout(grpS)
        self.stack = QStackedWidget()
        self.pg_lsc  = LSCSettingsWidget()
        self.pg_siam = SIAMSettingsWidget()
        self.pg_ngam = QLabel("NGAM – placeholder")
        self.pg_ngam.setAlignment(Qt.AlignCenter)
        self.stack.addWidget(self.pg_lsc)    # 0
        self.stack.addWidget(self.pg_siam)   # 1
        self.stack.addWidget(self.pg_ngam)   # 2
        vsS.addWidget(self.stack)
        
        layout_settings.addWidget(grpS)
        self.tabs.addTab(tab_settings, "Processing Settings")
        
        # Tab 2: Column Mappings
        tab_mappings = QWidget()
        layout_mappings = QVBoxLayout(tab_mappings)
        layout_mappings.setContentsMargins(0, 0, 0, 0)
        
        self.grpM = QGroupBox("Column Mappings  (File → SIAM)")
        vsM = QVBoxLayout(self.grpM)
        
        hdrM = QHBoxLayout()
        self.btnResetCols = QPushButton("↺ Reset Mandatory Columns")
        self.btnResetCols.setToolTip(
            "Pre-fill the mandatory target fields for the current instrument.\n"
            "Enter the matching column header from your data file in 'Source Header'.")
        self.btnResetCols.clicked.connect(self._populate_mandatory_columns)
        hdrM.addWidget(self.btnResetCols)
        hdrM.addStretch()
        vsM.addLayout(hdrM)
        
        self.tblMap = QTableWidget(0, 5)
        self.tblMap.setHorizontalHeaderLabels([
            "Target Field", "Source Header (file)", "Is Net?", "Uncertainty Column", "Description / Notes"
        ])
        self.tblMap.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tblMap.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tblMap.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tblMap.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        vsM.addWidget(self.tblMap)
        
        rowM = QHBoxLayout()
        for label, slot in [("+ Add Row", self._add_map_row),
                             ("− Remove",  self._rem_map_row)]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            rowM.addWidget(b)
        rowM.addStretch()
        vsM.addLayout(rowM)
        
        layout_mappings.addWidget(self.grpM)
        self.tabs.addTab(tab_mappings, "Column Mappings")
        
        vr.addWidget(self.tabs, stretch=3)

        # Bottom buttons
        bot = QHBoxLayout()
        
        # Cancel button (left side)
        self.btnCancel = QPushButton("Cancel")
        self.btnCancel.clicked.connect(self._cancel_edit)
        self.btnCancel.setStyleSheet("padding:6px 12px;")
        bot.addWidget(self.btnCancel)
        
        bot.addStretch()
        
        # Save and Select buttons (right side)
        self.btnSave   = QPushButton("💾  Save Protocol")
        self.btnSelect = QPushButton("✔  Select && Close")
        
        self.btnSave.setStyleSheet(  "background:#e1f5fe;font-weight:bold;padding:6px 12px;")
        self.btnSelect.setStyleSheet("background:#e8f5e9;font-weight:bold;padding:6px 12px;")
        self.btnSave.clicked.connect(  self._save_protocol)
        self.btnSelect.clicked.connect(self._select_and_close)
        bot.addWidget(self.btnSave)
        bot.addWidget(self.btnSelect)
        
        vr.addLayout(bot)

        spl = QSplitter(Qt.Horizontal)
        spl.addWidget(left); spl.addWidget(right); spl.setSizes([300, 820])
        root.addWidget(spl)

        self._on_module_changed(self.cmbModule.currentText())
        
        # Set initial button states (read-only until user clicks New/Edit)
        self._edit_mode = False
        self._update_button_states()

    # ── slots ─────────────────────────────────────────────────────────────────

    def _setup_source_header_combos(self, headers):
        """
        Convert Source Header to combos with auto-suggestions.
        Priority: 1) Existing saved value, 2) Auto-suggest from COLUMN_ALIASES
        """
        if not headers:
            return
        
        for row in range(self.tblMap.rowCount()):
            target_item = self.tblMap.item(row, 0)
            if not target_item:
                continue
            
            target_field = target_item.text()
            
            # Check for existing value (from DB or previous combo)
            existing_value = None
            
            # Check if already a combo
            existing_combo = self.tblMap.cellWidget(row, 1)
            if isinstance(existing_combo, QComboBox):
                existing_value = existing_combo.currentText()
                if existing_value == "(select from file)":
                    existing_value = None
            
            # Check if text item with saved value
            if not existing_value:
                source_item = self.tblMap.item(row, 1)
                if source_item:
                    text = source_item.text().strip()
                    if text:
                        existing_value = text
            
            # Create combo with file headers
            combo = QComboBox()
            combo.addItem("(select from file)", "")
            
            for header in sorted(headers):
                combo.addItem(header, header)
            
            # Select value (priority: existing > auto-suggest)
            value_to_select = None
            is_from_db = False
            
            if existing_value:
                # Use saved/existing value (highest priority)
                value_to_select = existing_value
                is_from_db = True
            else:
                # Try auto-suggest using COLUMN_ALIASES
                if target_field in COLUMN_ALIASES:
                    aliases = COLUMN_ALIASES[target_field]
                    headers_lower = {h.lower(): h for h in headers}
                    
                    for alias in aliases:
                        if alias.lower() in headers_lower:
                            value_to_select = headers_lower[alias.lower()]
                            break
            
            # Set the selected value
            if value_to_select:
                idx = combo.findText(value_to_select)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                    
                    # Visual feedback
                    if is_from_db:
                        combo.setStyleSheet("background-color: #e3f2fd;")  # Blue = saved
                        combo.setToolTip(f"Saved: {value_to_select}")
                    else:
                        combo.setStyleSheet("background-color: #c8e6c9;")  # Green = auto-suggested
                        combo.setToolTip(f"Auto-suggested: {value_to_select}")
            
            # Set as cell widget
            self.tblMap.setCellWidget(row, 1, combo)

    def _setup_uncertainty_combos(self, headers):
        """
        Convert Uncertainty Column to combos with uncertainty-specific headers.
        Similar to _setup_source_header_combos but filters for uncertainty columns.
        """
        if not headers:
            return
        
        # Filter headers that might be uncertainty columns
        uncertainty_keywords = ['unc', 'err', 'error', 'std', 'sigma', 'uncertainty']
        unc_headers = [h for h in headers 
                      if any(kw in h.lower() for kw in uncertainty_keywords)]
        
        for row in range(self.tblMap.rowCount()):
            target_item = self.tblMap.item(row, 0)
            if not target_item:
                continue
            
            # Create combo
            combo = QComboBox()
            combo.addItem("(none)", "")
            
            # Add uncertainty headers first (filtered)
            for header in sorted(unc_headers):
                combo.addItem(header, header)
            
            # Add separator
            if unc_headers and len(headers) > len(unc_headers):
                combo.insertSeparator(combo.count())
            
            # Add all other headers
            for header in sorted(headers):
                if header not in unc_headers:
                    combo.addItem(header, header)
            
            # Set as cell widget
            self.tblMap.setCellWidget(row, 3, combo)  # Column 3 = uncertainty
                    
    def _on_module_changed(self, text: str):
        self.stack.setCurrentIndex({"LSC": 0, "SIAM": 1}.get(text, 2))
        
        # Update column mappings title dynamically
        if text == "LSC":
            self.grpM.setTitle("Column Mappings  (File → TRIMS)")
        elif text == "SIAM":
            self.grpM.setTitle("Column Mappings  (File → SIAM)")
        else:
            self.grpM.setTitle("Column Mappings  (File → System)")
        
        # Update file format combo based on module
        self._populate_file_formats(text)
    
    def _populate_file_formats(self, module: str):
        """Populate file format combo from database based on module."""
        self.cmbFileFormat.clear()
        
        try:
            from db_core import db_manager
            from sqlalchemy import text
            
            # Determine category based on module
            if module == "LSC":
                category = 3  # TRIMS
            elif module == "SIAM":
                category = 5  # SIAM
            else:
                self.cmbFileFormat.addItem("N/A")
                return
            
            with db_manager.get_connection() as conn:
                query = text("""
                    SELECT lngFormatID, strFormatName 
                    FROM GUItblFileFormat 
                    WHERE intCategory = :cat 
                    ORDER BY strFormatName
                """)
                rows = conn.execute(query, {"cat": category}).fetchall()
                
                for row in rows:
                    # Store ID as item data, show name as text
                    self.cmbFileFormat.addItem(row.strFormatName, row.lngFormatID)
                
                if self.cmbFileFormat.count() == 0:
                    self.cmbFileFormat.addItem("(No formats available)")
                    
        except Exception as e:
            logging.warning(f"Could not load file formats: {e}")
            self.cmbFileFormat.addItem("(Database error)")

    def _update_button_states(self):
        """Update button enabled/disabled states based on current mode"""
        if self._edit_mode:
            # EDITING: Enable save/cancel, disable list operations
            self.btnSave.setEnabled(True)
            self.btnCancel.setEnabled(True)
            self.btnSelect.setEnabled(False)
            
            # Disable list and CRUD buttons during edit
            self.lstProtocols.setEnabled(False)
            self.btnNew.setEnabled(False)
            self.btnEdit.setEnabled(False)
            self.btnDuplicate.setEnabled(False)
            self.btnDelete.setEnabled(False)
        else:
            # NOT EDITING: Enable list operations, disable save/cancel
            self.btnSave.setEnabled(False)
            self.btnCancel.setEnabled(False)
            
            # Enable list
            self.lstProtocols.setEnabled(True)
            
            # Enable CRUD based on selection
            has_selection = self.lstProtocols.currentItem() is not None
            self.btnSelect.setEnabled(has_selection)
            self.btnNew.setEnabled(True)
            self.btnEdit.setEnabled(has_selection)
            self.btnDuplicate.setEnabled(has_selection)
            self.btnDelete.setEnabled(has_selection)
    
    def _set_form_readonly(self, readonly: bool):
        """Set form fields to read-only or editable."""
        self.txtName.setReadOnly(readonly)
        self.cmbModule.setEnabled(not readonly)
        self.cmbFileFormat.setEnabled(not readonly)
        self.chkActive.setEnabled(not readonly)
        self.chkDefault.setEnabled(not readonly)
        self.txtDesc.setReadOnly(readonly)
        # self.stack.setEnabled(not readonly)
        
        if readonly:
            self.tblMap.setEditTriggers(QTableWidget.NoEditTriggers)
        else:
            self.tblMap.setEditTriggers(QTableWidget.AllEditTriggers)
            
        self.btnResetCols.setEnabled(not readonly)
        
        # Visual feedback
        if readonly:
            self.txtName.setStyleSheet("background-color: #f0f0f0;")
            self.txtDesc.setStyleSheet("background-color: #f0f0f0;")
        else:
            self.txtName.setStyleSheet("")
            self.txtDesc.setStyleSheet("")
    
    def _edit_protocol(self):
        """Enter edit mode for selected protocol."""
        item = self.lstProtocols.currentItem()
        if not item:
            return
        
        # Load protocol if not already loaded
        if self.current_id != item.data(Qt.UserRole):
            self._load_protocol(item)
        
        # Enter edit mode
        self._edit_mode = True
        self._update_button_states()

    def _cancel_edit(self):
        """Cancel edit mode and revert changes."""
        if not self._edit_mode:
            return
        
        reply = QMessageBox.question(
            self, "Cancel Changes",
            "Discard all changes and revert?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            # Reload from list or clear if new
            item = self.lstProtocols.currentItem()
            if item:
                self._load_protocol(item)
            else:
                self._clear_form()
            
            self._edit_mode = False
            self._update_button_states()
            
    def _populate_mandatory_columns(self):
        module = self.cmbModule.currentText()
        
        # Get mandatory columns based on module
        if module == "SIAM":
            widget = self.pg_siam
        elif module == "LSC":
            widget = self.pg_lsc
        else:
            QMessageBox.information(
                self, "No Columns", 
                f"No mandatory columns defined for {module}."
            )
            return
        
        if not hasattr(widget, 'get_mandatory_columns'):
            QMessageBox.warning(
                self, "Error",
                f"{module} settings widget missing get_mandatory_columns() method."
            )
            return
        
        mandatory = widget.get_mandatory_columns()
        if not mandatory:
            return
        
        # Clear table
        self.tblMap.setRowCount(0)
        
        # Populate rows
        for target_field, description in mandatory:
            r = self.tblMap.rowCount()
            self.tblMap.insertRow(r)
            
            # Target field (read-only, gray)
            it = QTableWidgetItem(target_field)
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            it.setBackground(Qt.lightGray)
            it.setToolTip(description)
            self.tblMap.setItem(r, 0, it)            
            # Source header (empty initially)
            self.tblMap.setItem(r, 1, QTableWidgetItem(""))            
            # Is Net checkbox
            chk = QTableWidgetItem()
            chk.setCheckState(Qt.Unchecked)
            self.tblMap.setItem(r, 2, chk)            
            # Uncertainty column (empty initially)
            self.tblMap.setItem(r, 3, QTableWidgetItem(""))           
            # Description
            self.tblMap.setItem(r, 4, QTableWidgetItem(description))
        
        # Auto-setup combos if headers were provided
        if self._file_headers:
            self._setup_source_header_combos(self._file_headers)
            self._setup_uncertainty_combos(self._file_headers)
            
            # Count auto-suggestions
            auto_count = sum(
                1 for r in range(self.tblMap.rowCount())
                if isinstance(self.tblMap.cellWidget(r, 1), QComboBox)
                and self.tblMap.cellWidget(r, 1).currentIndex() > 0
            )
            
            if auto_count > 0:
                logging.debug(
                    f"{auto_count} mappings auto-suggested from loaded file!"
                )
                
    def _detect_source_headers(self) -> dict:
        """
        Try to detect source column headers from parent's loaded data.
        Returns dict mapping target_field -> detected_source_header
        """
        detected = {}
        
        # Try multiple ways to get column names from parent
        file_columns = []
        
        # Method 1: Check if parent has 'data' attribute (DataFrame)
        if hasattr(self.parent(), 'data'):
            parent_data = self.parent().data
            if hasattr(parent_data, 'columns'):
                try:
                    file_columns = list(parent_data.columns)
                except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        
        # Method 2: Check if parent has 'vendor_table' attribute
        if not file_columns and hasattr(self.parent(), 'vendor_table'):
            vendor_table = self.parent().vendor_table
            if hasattr(vendor_table, 'columns'):
                try:
                    file_columns = list(vendor_table.columns)
                except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        
        # Method 3: Check if parent has 'current_data' attribute
        if not file_columns and hasattr(self.parent(), 'current_data'):
            current_data = self.parent().current_data
            if hasattr(current_data, 'columns'):
                try:
                    file_columns = list(current_data.columns)
                except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        
        if not file_columns:
            return detected  # No data loaded, return empty dict
        
        # Try to use siam_column_resolver for matching
        try:
            from siam_column_resolver import COLUMN_ALIASES
            
            # Normalize function for fuzzy matching
            import re
            def normalize(s):
                # Remove spaces, underscores, dashes, make lowercase
                return re.sub(r'[\s_-]+', '', str(s).lower())
            
            # For each target field, try to find matching source column
            for target_field in ["sample_id", "timestamp", "d18O", "dD", "d17O", 
                                 "d13C", "d15N", "water_conc", "injection_no", "H2O_conc"]:
                aliases = COLUMN_ALIASES.get(target_field, [])
                
                # Create normalized alias set
                alias_set = {normalize(a): a for a in aliases}
                
                # Find first matching column
                for col in file_columns:
                    norm_col = normalize(col)
                    if norm_col in alias_set:
                        detectedtarget_field = col
                        break
                    # Also check if the original alias matches exactly
                    if col in aliases:
                        detectedtarget_field = col
                        break
                        
        except ImportError:
            # Fallback: use simple exact matching with common column names
            common_mappings = {
                "sample_id": ["Identifier 1", "Sample ID", "ID", "Sample", "Vial"],
                "timestamp": ["Time", "DateTime", "Date", "Timestamp"],
                "d18O": ["Delta_18_16", "d18O", "d18o", "δ18O", "delta_18_16"],
                "dD": ["Delta_D_H", "dD", "δD", "delta_D_H", "Delta_2H_1H"],
                "water_conc": ["H2O", "H2O_ppm", "water_conc", "H2O_Mean"],
                "injection_no": ["Inj Nr", "Injection", "InjNr"],
            }
            
            for target, possible_names in common_mappings.items():
                for col in file_columns:
                    if col in possible_names:
                        detectedtarget = col
                        break
        except Exception as e:
            logging.debug(f"Auto-detection failed: {e}")
        
        return detected

    def refresh_list(self):
        self.lstProtocols.clear()
        
        # Determine module filter (respect restriction)
        if self._restrict_to_module and self._ctx_module:
            flt = self._ctx_module  # Locked to this module
        else:
            flt = self.cmbFilter.currentText()  # User can choose
        
        try:
            protocols = ProtocolManager.get_protocols(
                module_filter=flt if flt != "All" else None
            )
            if self._ctx_module == "LSC" and self._ctx_isotope and self._ctx_format:
                protocols = [
                    p for p in protocols
                    if (p.settings.get("isotope_id") == self._ctx_isotope and
                        p.settings.get("format_id")  == self._ctx_format)
                ]
            for p in protocols:
                label = f"[{p.module}]  {p.name}"
                if p.module == "SIAM":
                    label += f"  — {p.settings.get('instrument_name', '')}"
                
                # Add visual indicators
                if p.is_default:
                    label = "⭐ " + label  # Star for default
                
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, p.id)
                
                # Format default protocols
                if p.is_default:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                
                # Gray out inactive protocols
                if not p.is_active:
                    item.setForeground(Qt.gray)
                    item.setToolTip("Inactive protocol")
                
                self.lstProtocols.addItem(item)
                
        except Exception as e:
            logging.error(f"refresh_list: {e}", exc_info=True)
            QMessageBox.warning(self, "Load Error", str(e))

    def _filter_protocols(self, search_text: str):
        """Filter protocol list based on search text"""
        search_text = search_text.lower().strip()
        
        for i in range(self.lstProtocols.count()):
            item = self.lstProtocols.item(i)
            item_text = item.text().lower()
            
            # Show item if search is empty or matches
            should_show = not search_text or search_text in item_text
            item.setHidden(not should_show)
            
    def _select_protocol_by_id(self, protocol_id: int):
        """Select and load protocol by ID"""
        if not protocol_id:
            return
        
        # Find item with matching ID
        for i in range(self.lstProtocols.count()):
            item = self.lstProtocols.item(i)
            if item.data(Qt.UserRole) == protocol_id:
                # Select item
                self.lstProtocols.setCurrentItem(item)
                # Load protocol
                self._load_protocol(item)
                # Scroll to make it visible
                self.lstProtocols.scrollToItem(item)
                logging.info(f"Auto-selected protocol ID {protocol_id}")
                break
            
    def _load_protocol(self, item: QListWidgetItem):
        p = ProtocolManager.load_protocol(item.data(Qt.UserRole))
        if not p: return
        self._load_protocol_from_object(p)
        self._edit_mode = False  # Exit edit mode when loading
        self._update_button_states()
    
    def _load_protocol_from_object(self, p: Protocol):
        """Load a Protocol object into the UI (used when protocol= parameter provided)."""
        if not p: return
        self.current_id = p.id
        self.txtName.setText(p.name)
        self.txtDesc.setPlainText(p.description or "")
        
        # Set module (this triggers _on_module_changed to switch stack and populate file formats)
        with _BlockSignals(self.cmbModule):
            self.cmbModule.setCurrentText(p.module)
        self._on_module_changed(p.module)  # Manually trigger to switch settings widget
        
        # Load file format — canonical location is Protocol.format_id; settings keys are legacy
        fid = p.settings.get("file_format_id") or (p.format_id if hasattr(p, 'format_id') else None)
        if fid is not None:
            idx = self.cmbFileFormat.findData(int(fid))
            if idx >= 0:
                self.cmbFileFormat.setCurrentIndex(idx)
        elif "file_format_name" in p.settings:
            idx = self.cmbFileFormat.findText(p.settings["file_format_name"])
            if idx >= 0:
                self.cmbFileFormat.setCurrentIndex(idx)
        
        self.chkActive.setChecked(p.settings.get("is_active", p.is_active if hasattr(p, 'is_active') else True))
        self.chkDefault.setChecked(p.settings.get("is_default", False))
        
        # Now load settings into the appropriate widget (this will set isotope checkboxes)
        w = self.stack.currentWidget()
        if hasattr(w, "set_data"):
            w.set_data(p.settings)
        
        # Load column mappings (if any exist, use them; otherwise repopulate)
        if p.mappings:
            self.tblMap.setRowCount(0)
            for m in p.mappings:
                r = self.tblMap.rowCount()
                self.tblMap.insertRow(r)
                
                # Target field (read-only)
                target_item = QTableWidgetItem(m.target_field)
                target_item.setFlags(target_item.flags() & ~Qt.ItemIsEditable)
                target_item.setBackground(Qt.lightGray)
                self.tblMap.setItem(r, 0, target_item)
                
                # Source header (text initially, converted to combo below)
                self.tblMap.setItem(r, 1, QTableWidgetItem(m.source_header or ""))
                
                # Is Net
                chk = QTableWidgetItem()
                chk.setCheckState(Qt.Checked if m.is_net else Qt.Unchecked)
                self.tblMap.setItem(r, 2, chk)
                
                # Uncertainty column (NEW)
                unc_value = getattr(m, 'uncertainty_column', '') or ''
                self.tblMap.setItem(r, 3, QTableWidgetItem(unc_value))
                
                # Notes (moved to column 4)
                self.tblMap.setItem(r, 4, QTableWidgetItem(getattr(m, "notes", "") or ""))
            
            # Convert to combos if file headers available
            if self._file_headers and p.module in ("SIAM", "LSC"):
                self._setup_source_header_combos(self._file_headers)
                self._setup_uncertainty_combos(self._file_headers)
        else:
            # No mappings - repopulate with defaults based on current instrument
            if p.module in ("SIAM", "LSC"):
                self._populate_mandatory_columns()

    def _select_and_close(self, *_):
        item = self.lstProtocols.currentItem()
        if item:
            protocol = ProtocolManager.load_protocol(item.data(Qt.UserRole))
            self.selected_protocol = protocol
            self.protocol = protocol  # Also set protocol for dlg.protocol access
        self.accept()

    def _validate_protocol(self) -> tuple[bool, list[str]]:
        """
        Validate protocol before saving.
        Returns (is_valid, warnings_list)
        """
        warnings = []
        
        # Check name
        if not self.txtName.text().strip():
            warnings.append("⚠️ Protocol name is required")
            return False, warnings
        
        # Check column mappings
        mapped_count = 0
        for r in range(self.tblMap.rowCount()):
            source_widget = self.tblMap.cellWidget(r, 1)
            if isinstance(source_widget, QComboBox):
                sh = source_widget.currentText()
                if sh and sh not in ["(select from file)", ""]:
                    mapped_count += 1
        
        if mapped_count == 0:
            warnings.append("⚠️ No column mappings defined")
        elif mapped_count < 3 and self.cmbModule.currentText() in ("LSC", "SIAM"):
            warnings.append(f"ℹ️ Only {mapped_count} columns mapped (recommend at least 3)")
        
        # Check settings
        w = self.stack.currentWidget()
        if hasattr(w, "get_data"):
            settings = w.get_data()
            if not settings or len(settings) == 0:
                warnings.append("ℹ️ No settings configured")
        
        return True, warnings
    
    def _save_protocol(self):
        # Validate protocol
        is_valid, warnings = self._validate_protocol()
        
        if not is_valid:
            QMessageBox.warning(self, "Validation Error", "\n".join(warnings))
            return
        
        if warnings:
            # Show warnings but allow save
            msg = "Protocol has warnings:\n\n" + "\n".join(warnings) + "\n\nSave anyway?"
            reply = QMessageBox.question(self, "Validation Warnings", msg,
                                        QMessageBox.Yes | QMessageBox.No)
            if reply != QMessageBox.Yes:
                return        
        mod = self.cmbModule.currentText()
        format_id = self.cmbFileFormat.currentData()
        p   = Protocol(id=self.current_id, name=self.txtName.text().strip(),
                       module=mod, description=self.txtDesc.toPlainText().strip(), format_id=format_id)
        if not p.name:
            QMessageBox.warning(self, "Validation", "Protocol name is required."); return
        
        # Get settings from widget
        w = self.stack.currentWidget()
        p.settings = w.get_data() if hasattr(w, "get_data") else {}
        
        # Add metadata fields to settings
        p.settings["file_format_id"] = self.cmbFileFormat.currentData()  # Get ID from combo
        p.settings["file_format_name"] = self.cmbFileFormat.currentText()  # Get name for display
        p.settings["is_active"] = self.chkActive.isChecked()
        p.settings["is_default"] = self.chkDefault.isChecked()
        
        
        # Get isotope from appropriate source
        if mod == "SIAM" and hasattr(self.pg_siam, '_selected_isotopes'):
            selected_isos = self.pg_siam._selected_isotopes()
            if selected_isos:
                p.settings["isotope"] = ",".join(selected_isos)
        elif mod == "LSC":
            # For LSC, isotope is typically 3H but could be from settings
            # Just document what's in the primary_isotopes from settings
            p.settings["isotope"] = p.settings.get("primary_isotopes", "3H")
            
        # Column mappings
        mappings = []
        for r in range(self.tblMap.rowCount()):
            # Extract Target Field
            target_widget = self.tblMap.cellWidget(r, 0)
            if isinstance(target_widget, QComboBox):
                tf = target_widget.currentText()
            else:
                tf = (self.tblMap.item(r, 0) or QTableWidgetItem()).text().strip()
            
            # Extract Source Header from combo or text
            source_widget = self.tblMap.cellWidget(r, 1)
            if isinstance(source_widget, QComboBox):
                sh = source_widget.currentText()
                if sh in ["(select from file)", ...]:
                    sh = ""
            else:
                sh = (self.tblMap.item(r, 1) or QTableWidgetItem()).text().strip()
            
            # Is Net
            chk_item = self.tblMap.item(r, 2)
            is_net = chk_item.checkState() == Qt.Checked if chk_item else False
            
            # Uncertainty Column (NEW)
            unc_widget = self.tblMap.cellWidget(r, 3)
            if isinstance(unc_widget, QComboBox):
                unc_col = unc_widget.currentText()
                if unc_col == "(none)":
                    unc_col = ""
            else:
                unc_col = (self.tblMap.item(r, 3) or QTableWidgetItem()).text().strip()
            
            # Notes (moved to column 4)
            notes = (self.tblMap.item(r, 4) or QTableWidgetItem()).text().strip()
            
            # Create mapping with uncertainty
            mappings.append(ColumnMapping(
                target_field=tf,
                source_header=sh,
                is_net=is_net,
                uncertainty_column=unc_col,
                notes=notes
            ))
            
        p.mappings = mappings
        
        # Save is_active to Protocol object itself (not just settings)
        p.is_active = self.chkActive.isChecked()
        p.is_default = self.chkDefault.isChecked()
        
        try:
            self.current_id = ProtocolManager.save_protocol(p)
            p.id = self.current_id
            self.protocol = p
            self.selected_protocol = p
            QMessageBox.information(self, "Saved", f"Protocol '{p.name}' saved.")
            self.refresh_list()        
            self._select_protocol_by_id(self.current_id)
            
            self._edit_mode = False
            self._update_button_states()
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    def _clear_form(self):
        """Reset all form fields without changing edit mode."""
        self.current_id = -1
        self.txtName.clear()
        self.chkActive.setChecked(True)
        self.chkDefault.setChecked(False)
        self.txtDesc.clear()
        self.tblMap.setRowCount(0)
        self._on_module_changed(self.cmbModule.currentText())

    def _new_protocol(self):
        self._clear_form()
        # Populate mandatory columns for SIAM/LSC
        if self.cmbModule.currentText() in ("SIAM", "LSC"):
            self._populate_mandatory_columns()
            self.txtName.setText(self.cmbModule.currentText())
        self._edit_mode = True
        self._update_button_states()

    def _duplicate_protocol(self):
        item = self.lstProtocols.currentItem()
        if not item: return
        p = ProtocolManager.load_protocol(item.data(Qt.UserRole))
        if not p: return
        p.id = -1; p.name += " (copy)"; self.current_id = -1
        self.txtName.setText(p.name); self.txtDesc.setPlainText(p.description or "")
        self.cmbModule.setCurrentText(p.module)
        w = self.stack.currentWidget()
        if hasattr(w, "set_data"): w.set_data(p.settings)

    def _delete_protocol(self):
        item = self.lstProtocols.currentItem()
        if not item: return
        if QMessageBox.question(self, "Delete", f"Delete '{item.text()}'?",
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            try:
                # Soft delete: set is_active = False
                p = ProtocolManager.load_protocol(item.data(Qt.UserRole))
                if p:
                    p.is_active = False
                    ProtocolManager.save_protocol(p)
                    self.refresh_list()
            except Exception as e:
                QMessageBox.critical(self, "Delete Error", str(e))

    def _add_map_row(self):
        r = self.tblMap.rowCount()
        self.tblMap.insertRow(r)
        for c in range(5):  # ← Changed from 4 to 5
            self.tblMap.setItem(r, c, QTableWidgetItem(""))
        chk = QTableWidgetItem()
        chk.setCheckState(Qt.Unchecked)
        self.tblMap.setItem(r, 2, chk)

    def _rem_map_row(self):
        self.tblMap.removeRow(self.tblMap.currentRow())

# Legacy aliases — existing callers unchanged
ProtocolManagerDialog = ProtocolGUI
ProtocolEditorDialog  = ProtocolGUI


if __name__ == "__main__":
    import os
    from PyQt5.QtGui import QFont
    from PyQt5.QtCore import Qt
    
    # Enable high-DPI scaling
    os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    if hasattr(Qt, 'AA_DontUseNativeDialogs'):
        QApplication.setAttribute(Qt.AA_DontUseNativeDialogs, True)
    app = QApplication(sys.argv)

    # Set larger default font for 4K
    font = QFont()
    font.setPointSize(9)  # Increase from default 8-9
    app.setFont(font)

    logging.basicConfig(level=logging.INFO)
    ProtocolGUI().show()
    sys.exit(app.exec_())