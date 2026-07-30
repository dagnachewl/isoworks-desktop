"""
procedure_management_gui.py — Analysis procedure management panel for IsoWorks.
Provides ProcedureManagementWidget with CRUD operations on the AnalysisProcedure
table, plus launchers for template and measurable configuration dialogs.
"""
import sys
import logging
import getpass
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QComboBox, QLineEdit,
    QPushButton, QCheckBox, QLabel, QFormLayout,
    QMessageBox, QTabWidget, QToolButton, QTextEdit
)
from PyQt5.QtCore import Qt

# --- NEW DB IMPORTS ---
from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from help_browser import make_help_button
from settings_style import BTN_SS, BTN_ADD_SS, BTN_DEL_SS

# --- Dialog Imports (Lazy) ---
try: from siam_template_editor_gui import SiamTemplateEditorDialog
except ImportError: SiamTemplateEditorDialog = None

try: from lsc_template_editor_gui import LscTemplateEditorDialog
except ImportError: LscTemplateEditorDialog = None
try: from siam_processor_config_gui import MeasurablesEditorDialog, PostProcessingDialog
except ImportError: MeasurablesEditorDialog = None; PostProcessingDialog = None
try: from enrichment_config_gui import EnrichmentConfigDialog
except ImportError: EnrichmentConfigDialog = None

try: from enrichment_template_editor_gui import EnrichmentTemplateEditorDialog
except ImportError: EnrichmentTemplateEditorDialog = None

try: from ngam_ng_template_editor import NGSeqTemplateEditorDialog
except ImportError: NGSeqTemplateEditorDialog = None

try: from ams_wheel_template_editor_gui import AmsWheelTemplateEditorDialog
except ImportError: AmsWheelTemplateEditorDialog = None

class ProcedureManagementWidget(QWidget):
    """
    CRUD operations for AnalysisProcedure table.
    UI restored to explicit layout style. DB updated to SQLAlchemy.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_procedure_id = None
        self.is_new_record = False
        
        self.main_layout = QVBoxLayout(self)        
        
        # 1. Action Buttons
        self.main_layout.addLayout(self._create_action_buttons())
        
        # 2. Filter & Selection
        self.main_layout.addWidget(self._create_filter_panel())
        self.main_layout.addWidget(self._create_selection_panel())
        
        self.main_layout.addSpacing(10)
        
        # 3. Detail Panel (Tabs)
        self.main_layout.addWidget(self._create_detail_panel(), 1)
        
        self._connect_signals()
        
        # Startup Load
        try:
            db_manager.get_engine()
            self.load_module_combo()
        except Exception as e:
            logging.error(f"Startup DB Error: {e}")
            
        self._set_form_read_only(True)

    # --- Helpers ---
    def _sql_concat(self, *args):
        dialect = getattr(db_manager, 'dialect', 'SQL_SERVER')
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')): parts.append(arg)
            else: parts.append(f"CAST({arg} AS NVARCHAR(255))" if dialect == "SQL_SERVER" else str(arg))
        sep = " + " if dialect == "SQL_SERVER" else " || "
        return sep.join(parts)

    def _sql_bool(self, value):
        return "1" if value else "0"

    # --- UI Creation (Explicit, no loops) ---
    def _create_action_buttons(self):
        l = QHBoxLayout()
        self.btnNew = QPushButton("New"); self.btnNew.setStyleSheet(BTN_ADD_SS)
        self.btnDuplicate = QPushButton("Duplicate"); self.btnDuplicate.setStyleSheet(BTN_SS)
        self.btnEdit = QPushButton("Edit"); self.btnEdit.setStyleSheet(BTN_SS)
        self.btnSave = QPushButton("Save"); self.btnSave.setStyleSheet(BTN_ADD_SS)
        self.btnCancel = QPushButton("Cancel"); self.btnCancel.setStyleSheet(BTN_SS)
        self.btnDelete = QPushButton("Delete"); self.btnDelete.setStyleSheet(BTN_DEL_SS)
        
        l.addStretch(1)
        l.addWidget(self.btnNew)
        l.addWidget(self.btnDuplicate)
        l.addWidget(self.btnEdit)
        l.addWidget(self.btnSave)
        l.addWidget(self.btnCancel)
        l.addWidget(self.btnDelete)
        l.addWidget(make_help_button(self, "procedure_mgmt"))
        return l

    def _create_filter_panel(self):
        g = QGroupBox("Filter")
        f = QFormLayout(g)
        self.cmbModule = QComboBox()
        self.cmbCategory = QComboBox()
        f.addRow("Module:", self.cmbModule)
        f.addRow("Category:", self.cmbCategory)
        return g

    def _create_selection_panel(self):
        g = QGroupBox("Selection")
        l = QHBoxLayout(g)
        l.addWidget(QLabel("Select:"))
        self.procedure_selection_list = QComboBox()
        l.addWidget(self.procedure_selection_list, 1)
        
        self.btnMoveBack = QToolButton(); self.btnMoveBack.setText("<")
        self.btnMoveForward = QToolButton(); self.btnMoveForward.setText(">")
        l.addWidget(self.btnMoveBack)
        l.addWidget(self.btnMoveForward)
        return g

    def _create_detail_panel(self):
        self.tabs = QTabWidget()
        
        # --- Tab 1: Main Details ---
        t1 = QWidget()
        f1 = QFormLayout(t1)
        
        self.txtProcedureID = QLineEdit()
        self.txtProcedureName = QLineEdit()
        self.cmbDefaultDeviceID = QComboBox()
        self.txtEductMaterial = QLineEdit()
        self.txtProductMaterial = QLineEdit()
        self.txtNumberOfSamples = QLineEdit()
        self.txtSampleSize = QLineEdit()
        self.chkIsVirtual = QCheckBox("Is a virtual procedure?")
        self.chkIsObsolete = QCheckBox("Procedure is Inactive")
        
        f1.addRow("Procedure ID:", self.txtProcedureID)
        f1.addRow("Procedure Name:", self.txtProcedureName)
        f1.addRow("Default Device:", self.cmbDefaultDeviceID)
        f1.addRow("Starting Material:", self.txtEductMaterial)
        f1.addRow("Product Material:", self.txtProductMaterial)
        f1.addRow("Max. # of Samples:", self.txtNumberOfSamples)
        f1.addRow("Sample Size (mL):", self.txtSampleSize)
        f1.addRow("", self.chkIsVirtual)
        f1.addRow("", self.chkIsObsolete)
        
        self.txtCreateDateStamp = QLineEdit(); self.txtCreateUserStamp = QLineEdit()
        self.txtModifDateStamp = QLineEdit(); self.txtModifUserStamp = QLineEdit()
        f1.addRow("Created:", self.txtCreateDateStamp)
        f1.addRow("Created By:", self.txtCreateUserStamp)
        f1.addRow("Modified:", self.txtModifDateStamp)
        f1.addRow("Modified By:", self.txtModifUserStamp)
        self.tabs.addTab(t1, "Main Details")
        
        # --- Tab 2: Configuration ---
        t2 = QWidget(); v2 = QVBoxLayout(t2)
        f2 = QFormLayout()
        self.cmbAnalysisImportFormat = QComboBox()
        self.cmbSampleExportFormat = QComboBox()
        self.txtMeasurablesList = QLineEdit()

        f2.addRow("Import Format:", self.cmbAnalysisImportFormat)
        f2.addRow("Export Format:", self.cmbSampleExportFormat)
        f2.addRow("Measurables:", self.txtMeasurablesList)
        v2.addLayout(f2)
        
        # SI Config Group
        self.si_config_group = QGroupBox("SI Procedure Configuration")
        sif = QFormLayout(self.si_config_group)
        self.txtNumFloatingRef = QLineEdit()
        self.cmbFloatingRefSample = QComboBox()
        self.chkEnrichedSample = QCheckBox("Enriched Samples?")
        self.btnPostProcessing = QPushButton("Manage Post-Processing Steps...")
        self.btnPostProcessing.clicked.connect(self.on_edit_post_processing)
        sif.addRow("# Floating Refs:", self.txtNumFloatingRef)
        sif.addRow("Floating Sample ID:", self.cmbFloatingRefSample)
        sif.addRow("", self.chkEnrichedSample)
        sif.addRow("Corrections:", self.btnPostProcessing)
        v2.addWidget(self.si_config_group)
        
        # Enrichment Group
        self.enrichment_config_group = QGroupBox("Enrichment Configuration")
        enrich_layout = QFormLayout(self.enrichment_config_group)
        self.cmbSpikeID = QComboBox()
        self.txtNumberOfSpike = QLineEdit()
        self.txtNumberOfDeadWater = QLineEdit()
        self.txtNumberOfLabAir = QLineEdit()
        self.chkHasDeuteriumMethod = QCheckBox("Uses Deuterium Method")
        self.txtTargetWaterMass = QLineEdit()
        self.txtNa2O2Mass = QLineEdit()
        self.txtAmpereHour = QLineEdit()
        
        enrich_layout.addRow("Spike ID:", self.cmbSpikeID)
        enrich_layout.addRow("Number of Spikes:", self.txtNumberOfSpike)
        enrich_layout.addRow("Number of Dead Water:", self.txtNumberOfDeadWater)
        enrich_layout.addRow("# Lab Moisture:", self.txtNumberOfLabAir)
        enrich_layout.addRow("", self.chkHasDeuteriumMethod)
        enrich_layout.addRow("Target Water Vol.:", self.txtTargetWaterMass)
        enrich_layout.addRow("Na₂O₂ Mass:", self.txtNa2O2Mass)
        enrich_layout.addRow("Ampere Hours:", self.txtAmpereHour)
        v2.addWidget(self.enrichment_config_group)
        
        # LSC Config Group
        self.lsc_config_group = QGroupBox("LSC Configuration")
        lsc = QFormLayout(self.lsc_config_group)
        self.txtCocktailSize = QLineEdit()
        self.cmbCocktailType = QComboBox()
        self.txtNumberOfCycles = QLineEdit()
        self.txtNumberOfCycleRepeats = QLineEdit()
        self.txtCycleLength = QLineEdit()
        self.txtCycleMaxCounts = QLineEdit()
        
        lsc.addRow("Cocktail Size (mL):", self.txtCocktailSize)
        lsc.addRow("Cocktail Type:", self.cmbCocktailType)
        lsc.addRow("Number of Cycles:", self.txtNumberOfCycles)
        lsc.addRow("Cycle Repeats:", self.txtNumberOfCycleRepeats)
        lsc.addRow("Cycle Length (min):", self.txtCycleLength)
        lsc.addRow("Max Counts:", self.txtCycleMaxCounts)
        v2.addWidget(self.lsc_config_group)

        # Sub-Dialog Buttons
        btns = QHBoxLayout()
        self.btnEditTemplate = QPushButton("Edit Template...")
        self.btnEditTemplate.clicked.connect(self.on_edit_load_list)
        self.btnEditMeasurables = QPushButton("Edit Measurables...")
        self.btnEditMeasurables.clicked.connect(self.on_edit_measurables)
        self.btnEditConfig = QPushButton("Edit Config...")
        self.btnEditConfig.clicked.connect(self.on_edit_config)
        
        btns.addWidget(self.btnEditTemplate)
        btns.addWidget(self.btnEditMeasurables)
        btns.addWidget(self.btnEditConfig)
        v2.addLayout(btns)
        v2.addStretch(1)
        self.tabs.addTab(t2, "Configuration")
        
        # --- Tab 3: Notes ---
        t3 = QWidget(); f3 = QFormLayout(t3)
        self.txtRemarks = QTextEdit()
        self.txtReportingTextMemo = QTextEdit()
        f3.addRow("Remarks:", self.txtRemarks)
        f3.addRow("Reporting Footnote:", self.txtReportingTextMemo)
        self.tabs.addTab(t3, "Notes")
        
        return self.tabs

    def _connect_signals(self):
        self.cmbModule.currentIndexChanged.connect(self.on_module_changed)
        self.cmbCategory.currentIndexChanged.connect(self.on_category_changed)
        self.procedure_selection_list.currentIndexChanged.connect(self.on_procedure_selected)
        
        self.btnNew.clicked.connect(self.on_new)
        self.btnDuplicate.clicked.connect(self.on_duplicate)
        self.btnEdit.clicked.connect(self.on_edit)
        self.btnSave.clicked.connect(self.on_save)
        self.btnCancel.clicked.connect(self.on_cancel)
        self.btnDelete.clicked.connect(self.on_delete)
        
        self.btnMoveBack.clicked.connect(self.on_move_back_clicked)
        self.btnMoveForward.clicked.connect(self.on_move_forward_clicked)

    # --- Data Loading (Updated for SQLAlchemy) ---
    
    def load_module_combo(self):
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT ID, ModuleName FROM Module ORDER BY ID"))
                self.cmbModule.clear()
                self.cmbModule.addItem("- Select -", None)
                for row in res:
                    self.cmbModule.addItem(row[1], row[0])
                idx = self.cmbModule.findData(2)
                if idx > -1: self.cmbModule.setCurrentIndex(idx)
        except Exception as e:
            logging.error(f"Load module failed: {e}")

    def on_module_changed(self):
        mid = self.cmbModule.currentData()
        self.cmbCategory.clear()
        self.cmbCategory.addItem("- Select -", None)
        if not mid: return
        try:
            with db_manager.get_connection() as conn:
                sql = f"""SELECT ID, {db_manager.sql_concat('ID', "': '", 'sName')} FROM Job_Procedure WHERE ModuleID = :mid ORDER BY ID"""
                for row in conn.execute(text(sql), {"mid": mid}):
                    self.cmbCategory.addItem(row[1], row[0])
                idx = self.cmbCategory.findData(2)
                if idx > -1: self.cmbCategory.setCurrentIndex(idx)                    
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def on_category_changed(self):
        cid = self.cmbCategory.currentData()
        self.procedure_selection_list.clear()
        if not cid: return
        try:
            with db_manager.get_connection() as conn:
                sql = "SELECT ProcedureID, ProcedureName FROM AnalysisProcedure WHERE CategoryID = :cid ORDER BY ProcedureID"
                for row in conn.execute(text(sql), {"cid": cid}):
                    self.procedure_selection_list.addItem(f"{row[0]}: {row[1]}", row[0])
                idx = self.procedure_selection_list.findData(2)
                if idx > -1:
                    self.procedure_selection_list.setCurrentIndex(idx)
            self.load_dependent_combos(cid)
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        # Always populate the form for whichever item landed as current,
        # because setCurrentIndex(0) suppresses currentIndexChanged when
        # the combo was already at index 0 after clear().
        self.on_procedure_selected()

    def load_dependent_combos(self, cid):
        try:
            with db_manager.get_connection() as conn:
                # Devices
                sql = f"""SELECT EquipmentID, {db_manager.sql_concat("COALESCE(Identifier, '')", "' --- '", 'EquipmentName')} FROM Equipment WHERE CategoryID = :cid ORDER BY EquipmentID"""
                self.cmbDefaultDeviceID.clear(); self.cmbDefaultDeviceID.addItem("", None)
                for r in conn.execute(text(sql), {"cid": cid}): self.cmbDefaultDeviceID.addItem(r[1], r[0])
                
                # Floating Ref Sample (SI)
                if cid == 5:
                    sql_ref = f"""SELECT SampleID, {db_manager.sql_concat('Prefix', "'-'", 'SampleID', "': '", 'sName')} FROM Sample WHERE SampleType <> 0 AND MediaID = 1 ORDER BY SampleID"""
                    self.cmbFloatingRefSample.clear(); self.cmbFloatingRefSample.addItem("", None)
                    for r in conn.execute(text(sql_ref)): self.cmbFloatingRefSample.addItem(r[1], r[0])
                
                # LSC Cocktails
                if cid == 3:
                    res = conn.execute(text("SELECT lngScintillantID, strScintillantName FROM GUItblScintillationCocktail ORDER BY lngScintillantID"))
                    self.cmbCocktailType.clear(); self.cmbCocktailType.addItem("", None)
                    for r in res: self.cmbCocktailType.addItem(r[1], r[0])
                    
                # Enrichment Spikes
                if cid == 2:
                    res = conn.execute(text("SELECT R.SampleID, S.sName FROM ReferenceControl R JOIN Sample S ON R.SampleID=S.SampleID WHERE S.SampleType IN (3,4)"))
                    self.cmbSpikeID.clear(); self.cmbSpikeID.addItem("", None)
                    for r in res: self.cmbSpikeID.addItem(f"{r[0]} - {r[1]}", r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def on_procedure_selected(self):
        pid = self.procedure_selection_list.currentData()
        self.populate_form(pid)
        self._set_form_read_only(True)
        
    def populate_form(self, pid):
        if not pid: self._clear_form_fields(); return
        self.current_procedure_id = pid
        try:
            with db_manager.get_connection() as conn:
                # Main Data
                row = conn.execute(text("SELECT * FROM AnalysisProcedure WHERE ProcedureID = :pid"), {"pid": pid}).fetchone()
                if row:
                    self.txtProcedureID.setText(str(row.ProcedureID))
                    self.txtProcedureName.setText(row.ProcedureName or "")
                    self.cmbDefaultDeviceID.setCurrentIndex(self.cmbDefaultDeviceID.findData(row.DefaultDeviceID))
                    self.txtEductMaterial.setText(row.EductMaterial or "")
                    self.txtProductMaterial.setText(row.ProductMaterial or "")
                    self.txtNumberOfSamples.setText(str(row.NumberOfSamples or ""))
                    self.txtSampleSize.setText(str(row.SampleSize or ""))
                    self.chkIsVirtual.setChecked(bool(row.IsVirtual))
                    self.chkIsObsolete.setChecked(bool(row.IsObsolete))
                    self.txtMeasurablesList.setText(row.MeasurablesList or "")
                    self.txtRemarks.setText(row.Remarks or "")
                    self.txtReportingTextMemo.setText(row.ReportingTextMemo or "")
                    
                    # Timestamps
                    self.txtCreateDateStamp.setText(str(row.CreateDateStamp) if row.CreateDateStamp else "")
                    self.txtCreateUserStamp.setText(row.CreateUserStamp or "")
                    self.txtModifDateStamp.setText(str(row.ModifDateStamp) if row.ModifDateStamp else "")
                    self.txtModifUserStamp.setText(row.ModifUserStamp or "")
                
                # Category 2: Enrichment
                if self.cmbCategory.currentData() == 2:
                    erow = conn.execute(text("SELECT * FROM EnrichmentProcedure WHERE ProcedureID=:pid"), {"pid": pid}).fetchone()
                    if erow:
                        self.cmbSpikeID.setCurrentIndex(self.cmbSpikeID.findData(erow.SpikeID))
                        self.txtNumberOfSpike.setText(str(erow.NumberOfSpike or ""))
                        self.txtNumberOfDeadWater.setText(str(erow.NumberOfDeadWater or ""))
                        self.txtNumberOfLabAir.setText(str(erow.NumberOfLabAir or ""))
                        self.chkHasDeuteriumMethod.setChecked(bool(erow.HasDeuteriumMethod))
                        self.txtTargetWaterMass.setText(str(erow.TargetWaterMass or ""))
                        self.txtNa2O2Mass.setText(str(erow.Na2O2Mass or ""))
                        self.txtAmpereHour.setText(str(erow.AmpereHour or ""))

                # Category 3: LSC
                elif self.cmbCategory.currentData() == 3:
                    lrow = conn.execute(text("SELECT * FROM LSCProcedure WHERE ProcedureID=:pid"), {"pid": pid}).fetchone()
                    if lrow:
                        self.txtCocktailSize.setText(str(lrow.CocktailSize or ""))
                        self.cmbCocktailType.setCurrentIndex(self.cmbCocktailType.findData(lrow.CocktailType))
                        self.txtNumberOfCycles.setText(str(lrow.NumberOfCycles or ""))
                        self.txtNumberOfCycleRepeats.setText(str(lrow.NumberOfCycleRepeats or ""))
                        self.txtCycleLength.setText(str(lrow.CycleLength or ""))
                        self.txtCycleMaxCounts.setText(str(lrow.CycleMaxCounts or ""))

                # Category 5: SI
                elif self.cmbCategory.currentData() == 5:
                    srow = conn.execute(text("SELECT int_NumberFloatingRef, lng_SampleFloatingRef, EnrichedSample FROM SIProcedure WHERE ProcedureID=:pid"), {"pid": pid}).fetchone()
                    if srow:
                        self.txtNumFloatingRef.setText(str(srow.int_NumberFloatingRef or ""))
                        self.cmbFloatingRefSample.setCurrentIndex(self.cmbFloatingRefSample.findData(srow.lng_SampleFloatingRef))
                        self.chkEnrichedSample.setChecked(bool(srow.EnrichedSample))
                        
            self._update_config_tab(self.cmbCategory.currentData())
        except Exception as e: logging.error(f"Load failed: {e}")

    # --- Actions ---
    def on_save(self):
        if not self.txtProcedureName.text(): return
        try:
            with db_manager.get_connection() as conn:
                # 1. Save Main
                params = {
                    "name": self.txtProcedureName.text(), "did": self.cmbDefaultDeviceID.currentData(),
                    "mat1": self.txtEductMaterial.text(), "mat2": self.txtProductMaterial.text(),
                    "num": to_int_or_none(self.txtNumberOfSamples), "size": to_float_or_none(self.txtSampleSize),
                    "virt": self.chkIsVirtual.isChecked(), "obs": self.chkIsObsolete.isChecked(),
                    "rem": self.txtRemarks.toPlainText(), "rep": self.txtReportingTextMemo.toPlainText(),
                    "now": datetime.now(), "user": getpass.getuser()
                }
                
                if self.is_new_record:
                    pid = int(self.txtProcedureID.text())
                    self.current_procedure_id = pid
                    params["pid"] = pid; params["cid"] = self.cmbCategory.currentData()
                    sql = "INSERT INTO AnalysisProcedure (ProcedureID, ProcedureName, CategoryID, DefaultDeviceID, EductMaterial, ProductMaterial, NumberOfSamples, SampleSize, IsVirtual, IsObsolete, Remarks, ReportingTextMemo, CreateDateStamp, CreateUserStamp, ModifDateStamp, ModifUserStamp) VALUES (:pid, :name, :cid, :did, :mat1, :mat2, :num, :size, :virt, :obs, :rem, :rep, :now, :user, :now, :user)"
                    conn.execute(text(sql), params)
                else:
                    params["pid"] = self.current_procedure_id
                    sql = "UPDATE AnalysisProcedure SET ProcedureName=:name, DefaultDeviceID=:did, EductMaterial=:mat1, ProductMaterial=:mat2, NumberOfSamples=:num, SampleSize=:size, IsVirtual=:virt, IsObsolete=:obs, Remarks=:rem, ReportingTextMemo=:rep, ModifDateStamp=:now, ModifUserStamp=:user WHERE ProcedureID=:pid"
                    conn.execute(text(sql), params)
                
                # 2. Save Sub-tables
                cid = self.cmbCategory.currentData()
                pid = self.current_procedure_id
                
                if cid == 5: # SI
                    exists = conn.execute(text("SELECT 1 FROM SIProcedure WHERE ProcedureID=:pid"), {"pid": pid}).fetchone()
                    sp = {"num": to_int_or_none(self.txtNumFloatingRef), "ref": self.cmbFloatingRefSample.currentData(), "enr": self.chkEnrichedSample.isChecked(), "pid": pid}
                    if exists: conn.execute(text("UPDATE SIProcedure SET int_NumberFloatingRef=:num, lng_SampleFloatingRef=:ref, EnrichedSample=:enr WHERE ProcedureID=:pid"), sp)
                    else: conn.execute(text("INSERT INTO SIProcedure (int_NumberFloatingRef, lng_SampleFloatingRef, EnrichedSample, ProcedureID) VALUES (:num, :ref, :enr, :pid)"), sp)

                elif cid == 3: # LSC
                    exists = conn.execute(text("SELECT 1 FROM LSCProcedure WHERE ProcedureID=:pid"), {"pid": pid}).fetchone()
                    lp = {
                        "sz": to_float_or_none(self.txtCocktailSize), "typ": self.cmbCocktailType.currentData(),
                        "cyc": to_int_or_none(self.txtNumberOfCycles), "rep": to_int_or_none(self.txtNumberOfCycleRepeats),
                        "len": to_int_or_none(self.txtCycleLength), "max": to_int_or_none(self.txtCycleMaxCounts), "pid": pid
                    }
                    if exists: conn.execute(text("UPDATE LSCProcedure SET CocktailSize=:sz, CocktailType=:typ, NumberOfCycles=:cyc, NumberOfCycleRepeats=:rep, CycleLength=:len, CycleMaxCounts=:max WHERE ProcedureID=:pid"), lp)
                    else: conn.execute(text("INSERT INTO LSCProcedure (CocktailSize, CocktailType, NumberOfCycles, NumberOfCycleRepeats, CycleLength, CycleMaxCounts, ProcedureID) VALUES (:sz, :typ, :cyc, :rep, :len, :max, :pid)"), lp)
                
                elif cid == 2: # Enrichment
                    exists = conn.execute(text("SELECT 1 FROM EnrichmentProcedure WHERE ProcedureID=:pid"), {"pid": pid}).fetchone()
                    ep = {
                        "sid": self.cmbSpikeID.currentData(), "nsp": to_int_or_none(self.txtNumberOfSpike),
                        "ndw": to_int_or_none(self.txtNumberOfDeadWater), "nla": to_int_or_none(self.txtNumberOfLabAir),
                        "meth": self.chkHasDeuteriumMethod.isChecked(), "tar": to_float_or_none(self.txtTargetWaterMass),
                        "na": to_float_or_none(self.txtNa2O2Mass), "amp": to_float_or_none(self.txtAmpereHour), "pid": pid
                    }
                    if exists: conn.execute(text("UPDATE EnrichmentProcedure SET SpikeID=:sid, NumberOfSpike=:nsp, NumberOfDeadWater=:ndw, NumberOfLabAir=:nla, HasDeuteriumMethod=:meth, TargetWaterMass=:tar, Na2O2Mass=:na, AmpereHour=:amp WHERE ProcedureID=:pid"), ep)
                    else: conn.execute(text("INSERT INTO EnrichmentProcedure (SpikeID, NumberOfSpike, NumberOfDeadWater, NumberOfLabAir, HasDeuteriumMethod, TargetWaterMass, Na2O2Mass, AmpereHour, ProcedureID) VALUES (:sid, :nsp, :ndw, :nla, :meth, :tar, :na, :amp, :pid)"), ep)

                conn.commit()
                
            QMessageBox.information(self, "Success", "Saved.")
            self._set_form_read_only(True)
            self.on_category_changed() # Refresh list
            self.procedure_selection_list.setCurrentIndex(self.procedure_selection_list.findData(self.current_procedure_id))
            
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def on_new(self):
        if not self.cmbCategory.currentData(): return
        self.is_new_record=True; self._clear_form_fields(); self._set_form_read_only(False)
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT MAX(ProcedureID) FROM AnalysisProcedure WHERE CategoryID=:cid"), {"cid": self.cmbCategory.currentData()}).fetchone()
                self.txtProcedureID.setText(str((res[0] or 0) + 1))
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        self._update_config_tab(self.cmbCategory.currentData())
    
    def on_duplicate(self):
        if not self.current_procedure_id: return
        self.on_new() # Setup new ID
        # Pre-fill with old data
        self.populate_form(self.procedure_selection_list.currentData())
        # Reset ID to new
        # (Real duplicate logic would need more care to not overwrite the ID we just fetched)
        # For now, just re-fetching the NEW ID
        try:
             with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT MAX(ProcedureID) FROM AnalysisProcedure WHERE CategoryID=:cid"), {"cid": self.cmbCategory.currentData()}).fetchone()
                self.txtProcedureID.setText(str((res[0] or 0) + 1))
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        self.txtProcedureName.setText(self.txtProcedureName.text() + "_Copy")
        self.is_new_record = True
        self.current_procedure_id = None

    def on_edit(self): self.is_new_record=False; self._set_form_read_only(False)
    def on_cancel(self): self._set_form_read_only(True); self.populate_form(self.current_procedure_id)
    
    def on_delete(self):
        if QMessageBox.question(self, "Delete", "Delete Procedure?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No: return
        try:
            with db_manager.get_connection() as conn:
                pid = self.current_procedure_id
                conn.execute(text("DELETE FROM AnalysisProcedure_Template WHERE ProcedureID=:pid"), {"pid": pid})
                conn.execute(text("DELETE FROM SIProcedure WHERE ProcedureID=:pid"), {"pid": pid})
                conn.execute(text("DELETE FROM LSCProcedure WHERE ProcedureID=:pid"), {"pid": pid})
                conn.execute(text("DELETE FROM EnrichmentProcedure WHERE ProcedureID=:pid"), {"pid": pid})
                conn.execute(text("DELETE FROM AnalysisProcedure WHERE ProcedureID=:pid"), {"pid": pid})
                conn.commit()
            self.on_category_changed()
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def _set_form_read_only(self, ro):
        # Inputs
        for w in [self.txtProcedureName, self.txtEductMaterial, self.txtProductMaterial, self.txtNumberOfSamples, self.txtSampleSize, 
                  self.txtRemarks, self.txtReportingTextMemo, self.txtNumFloatingRef, self.txtCocktailSize, self.txtNumberOfCycles, 
                  self.txtNumberOfCycleRepeats, self.txtCycleLength, self.txtCycleMaxCounts, self.txtNumberOfSpike, self.txtNumberOfDeadWater,
                  self.txtNumberOfLabAir, self.txtTargetWaterMass, self.txtNa2O2Mass, self.txtAmpereHour]:
            w.setReadOnly(ro)
        
        # Combos/Checks
        for w in [self.cmbDefaultDeviceID, self.cmbAnalysisImportFormat, self.cmbSampleExportFormat, self.chkIsVirtual, self.chkIsObsolete,
                  self.cmbFloatingRefSample, self.chkEnrichedSample, self.cmbCocktailType, self.cmbSpikeID, self.chkHasDeuteriumMethod]:
            w.setEnabled(not ro)
            
        # FIX: Force boolean result to avoid NoneType error in setEnabled
        has_id = (self.current_procedure_id is not None)

        self.btnSave.setEnabled(not ro)
        self.btnCancel.setEnabled(not ro)
        self.btnEdit.setEnabled(ro and has_id)
        self.btnDelete.setEnabled(ro and has_id)
        self.btnDuplicate.setEnabled(ro and has_id)
        self.btnNew.setEnabled(ro)
        
        # Sub-dialogs allowed in edit mode
        for w in [self.btnEditTemplate, self.btnEditMeasurables, self.btnEditConfig, self.btnPostProcessing]:
             w.setEnabled(True) # Always allow opening (viewing)

    def _clear_form_fields(self):
        for w in [self.txtProcedureID, self.txtProcedureName, self.txtEductMaterial, self.txtProductMaterial, self.txtNumberOfSamples, self.txtSampleSize, self.txtRemarks, self.txtReportingTextMemo]: w.clear()
        self.chkIsObsolete.setChecked(False)
        self._clear_lsc_fields(); self._clear_si_fields(); self._clear_enrichment_fields()

    def _clear_lsc_fields(self):
        self.txtCocktailSize.clear(); self.cmbCocktailType.setCurrentIndex(0); self.txtNumberOfCycles.clear(); self.txtNumberOfCycleRepeats.clear(); self.txtCycleLength.clear(); self.txtCycleMaxCounts.clear()
    def _clear_si_fields(self):
        self.txtNumFloatingRef.clear(); self.cmbFloatingRefSample.setCurrentIndex(0); self.chkEnrichedSample.setChecked(False)
    def _clear_enrichment_fields(self):
        self.cmbSpikeID.setCurrentIndex(0); self.txtNumberOfSpike.clear(); self.txtNumberOfDeadWater.clear(); self.txtNumberOfLabAir.clear(); self.chkHasDeuteriumMethod.setChecked(False); self.txtTargetWaterMass.clear(); self.txtNa2O2Mass.clear(); self.txtAmpereHour.clear()

    def _update_config_tab(self, cid):
        self.si_config_group.setVisible(cid == 5)
        self.lsc_config_group.setVisible(cid == 3)
        self.enrichment_config_group.setVisible(cid == 2)
        # Disable Edit Template for categories that have no fixed load list
        _no_template = {1, 2, 10, 12, 13}
        self.btnEditTemplate.setEnabled(cid not in _no_template)

    def on_edit_load_list(self):
        cid = self.cmbCategory.currentData()
        if cid == 5:                            # SI -> SIAM template editor
            if SiamTemplateEditorDialog:
                device_name = self.cmbDefaultDeviceID.currentText()
                d = SiamTemplateEditorDialog(self.current_procedure_id, cid, device_name, self)
                if d.exec_(): self.populate_form(self.current_procedure_id)
        elif cid == 3:                            # Tritium -> LSC load list
            if LscTemplateEditorDialog:
                d = LscTemplateEditorDialog(self.current_procedure_id, cid, self)
                if d.exec_(): self.populate_form(self.current_procedure_id)
            else:
                from PyQt5.QtWidgets import QMessageBox
                QMessageBox.warning(self, 'Not Available',
                                    'lsc_template_editor_gui.py not found.')
        elif cid in (11, 14):                    # NGAM / Noble Gas -> NG seq template
            if NGSeqTemplateEditorDialog:
                d = NGSeqTemplateEditorDialog(self.current_procedure_id, self)
                if d.exec_(): self.populate_form(self.current_procedure_id)
            else:
                from PyQt5.QtWidgets import QMessageBox
                QMessageBox.warning(self, 'Not Available',
                                    'ngam_ng_template_editor.py not found.')
        elif cid == 19:                          # AMS_Measurement -> wheel template
            if AmsWheelTemplateEditorDialog:
                d = AmsWheelTemplateEditorDialog(self.current_procedure_id, self.txtProcedureName.text(), self)
                if d.exec_(): self.populate_form(self.current_procedure_id)
            else:
                from PyQt5.QtWidgets import QMessageBox
                QMessageBox.warning(self, 'Not Available',
                                    'ams_wheel_template_editor_gui.py not found.')

    def on_edit_measurables(self):
        if MeasurablesEditorDialog: MeasurablesEditorDialog(self.current_procedure_id, self).exec_()

    def on_edit_post_processing(self):
        if PostProcessingDialog: PostProcessingDialog(self.current_procedure_id, self).exec_()

    def on_edit_config(self):
        if EnrichmentConfigDialog and self.cmbCategory.currentData() == 2:
            EnrichmentConfigDialog(self.current_procedure_id, self.txtProcedureName.text(), self).exec_()

    def on_move_back_clicked(self):
        i = self.procedure_selection_list.currentIndex()
        if i > 0: self.procedure_selection_list.setCurrentIndex(i-1)
    def on_move_forward_clicked(self):
        i = self.procedure_selection_list.currentIndex()
        if i < self.procedure_selection_list.count() - 1: self.procedure_selection_list.setCurrentIndex(i+1)

def to_int_or_none(widget):
    try: return int(widget.text())
    except: return None
def to_float_or_none(widget):
    try: return float(widget.text())
    except: return None

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = ProcedureManagementWidget(); w.show()
    sys.exit(app.exec_())