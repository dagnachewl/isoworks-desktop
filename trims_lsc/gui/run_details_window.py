"""
TRIMS LSC Run Details Window

Displays run metadata, load list, and controls for managing LSC runs.
Extracted from trims_lsc_details_gui.py
"""
from __future__ import annotations

# trims_lsc_details_gui.py
import logging
import pandas as pd
import numpy as np

from datetime import datetime
from dataclasses import dataclass
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas

from PyQt5.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QLineEdit, QDateTimeEdit,
    QPushButton, QTableView, QLabel, QFileDialog,
    QTabWidget, QMessageBox, QComboBox,
    QAbstractItemView, QDesktopWidget
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QColor, QPainter, QFont, QPen
from PyQt5.QtCore import Qt, QRectF, QPointF, QDateTime, QSettings as _AppSettings
from PyQt5.QtPrintSupport import QPrinter, QPrintDialog

# Shared Utilities
from db_core import db_manager
from sqlalchemy import text
from sample_type_registry import STYPE_LSC_STD, STYPE_LSC_SPIKE_ENR
from sample_label_gui import LabelRenderer, analysis_spec, _make_qr_pixmap
from tray_print_renderer import draw_tray_cell as _shared_draw_tray_cell, render_tray_loadlist as _shared_render_tray

_label_renderer = LabelRenderer()

# ── Load-list PDF layout (mm) ─────────────────────────────────────────────────
_LL_COLS         = 3;   _LL_CARD_W         = 62.0; _LL_CARD_H         = 30.0
_LL_COLS_NOQR    = 4;   _LL_CARD_W_NOQR    = 46.0; _LL_CARD_H_NOQR    = 26.0
_LL_GAP_H = 3.0; _LL_GAP_V = 2.5; _LL_MARGIN_L = 8.0; _LL_MARGIN_T = 10.0; _LL_HDR_H = 14.0
from shared_utils import (
    compute_detection_limit_from_run, convert_activity_unit,
    get_standard_activity, format_person_name, get_current_user_id,
    get_global_value, check_employee_privilege, lower_detection_limit
)

# --- Strategy base for LSC parsers ---
from abc import ABC, abstractmethod
class LSCParserStrategy(ABC):
    """
    Strategy interface all LSC parsers must implement.
    - get_headers(): returns a list of source headers the UI can show/match
    - parse(mapping_dict, count_time_unit): returns a standardized DataFrame
      with columns: Position, VialPos, Cycle, Repeat, CountTime, CPM, QIP
    """
    def __init__(self, filepath: str):
        self.filepath = filepath

    @abstractmethod
    def get_headers(self) -> list:
        ...

    @abstractmethod
    def parse(self, mapping_dict: dict, count_time_unit: int = 1) -> pd.DataFrame:
        ...

    def _ensure_vialpos(self, df: pd.DataFrame, pos_col: str = 'Position') -> pd.DataFrame:
        """
        Creates/normalizes 'VialPos' for display and preserves the input label,
        without deciding run-order semantics. Safe for all formats.
        """
        if pos_col not in df.columns:
            # If no position column, create sequential
            df['Position'] = np.arange(1, len(df) + 1, dtype=int)
            df['VialPos']  = df['Position'].astype(str)
            return df

        s = df[pos_col].astype(str).str.strip()
        df['VialPos'] = s
        return df


    def _assign_positions(
        self,
        df: pd.DataFrame,
        *,
        vial_col: str = 'VialPos',
        mode: str = 'sequence',
        order_by: str | None = None
    ) -> pd.DataFrame:
        """
        Assigns integer 'Position' according to the chosen mode.

        Modes:
        - 'sequence': consecutive grouping (run order blocks)
                        Example: A01,A01,A02,A02,A01  -> 1,1,2,2,3
        - 'label'   : unique label factorization (ignores order)
                        Example: A01,A01,A02,A02,A01  -> 1,1,2,2,1

        order_by:
        Optional column name (e.g., 'EndTime') to sort *stable* BEFORE grouping.
        If None, uses file/read order (recommended for HIDEX List exports).
        """
        if vial_col not in df.columns:
            # Nothing to do; create sequential positions
            df['Position'] = np.arange(1, len(df) + 1, dtype=int)
            return df

        work = df
        # Stable sort if requested (keeps original order for ties)
        if order_by and order_by in df.columns:
            work = df.sort_values(by=[order_by, df.index.name or df.index], kind='mergesort')
        else:
            # preserve original read order
            work = df.copy()

        labels = work[vial_col].astype(str)

        if mode == 'label':
            # Factorize by unique label (old behavior)
            work['Position'] = pd.factorize(labels)[0] + 1

        elif mode == 'sequence':
            # Consecutive block grouping:
            # new block starts whenever label changes from previous row
            # NaNs handled by filling a sentinel that won't compare equal to real labels
            series = labels.fillna('__NA__')
            # new block flag = True where current != previous
            new_block = series.ne(series.shift(1, fill_value='__START__'))
            # cumsum across new_block -> 1,1,2,2,3 ... per sequence
            work['Position'] = new_block.cumsum().astype(int)

        else:
            raise ValueError("Unsupported mode for _assign_positions: use 'sequence' or 'label'")

        # Write back in original index order
        df = work.sort_index()
        return df


    def _standardize_cycles(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Ensures 'Cycle' and 'Repeat' exist and are coherent with run-order Position.

        Rules:
        - If 'Cycle' missing but 'Repeat' present -> Cycle := Repeat; Repeat := 1
        - If 'Cycle' still missing -> generate 1..N within each Position block
        """
        if 'Cycle' not in df.columns and 'Repeat' in df.columns:
            df['Cycle'] = df['Repeat']
            df['Repeat'] = 1

        if 'Cycle' not in df.columns:
            if 'Position' in df.columns:
                df['Cycle'] = df.groupby('Position').cumcount() + 1
            else:
                df['Cycle'] = np.arange(1, len(df) + 1, dtype=int)

        if 'Repeat' not in df.columns:
            df['Repeat'] = 1

        return df
    
    # ---- Common helpers shared by all strategies ----
    def _standardize_positions(self, df: pd.DataFrame, pos_col: str) -> pd.DataFrame:
        """
        Ensures Position (int for DB) and VialPos (string for tray display).
        If the source position is numeric -> Position stays numeric, VialPos=str(Position).
        If alphanumeric (e.g., A01 or 'Tray1-07') -> factorize order into Position, keep raw string in VialPos.
        """
        if pos_col not in df.columns:
            # If no explicit position column, create sequential positions
            df['Position'] = np.arange(1, len(df) + 1, dtype=int)
            df['VialPos']  = df['Position'].astype(str)
            return df

        s = df[pos_col].astype(str).str.strip()
        s_num = pd.to_numeric(s, errors='coerce')
        if s_num.notna().all():
            df['Position'] = s_num.astype(int)
            df['VialPos']  = df['Position'].astype(str)
        else:
            df['VialPos']  = s
            df['Position'] = pd.factorize(s)[0] + 1
        return df
    
    def _apply_time_unit(self, df: pd.DataFrame, count_time_unit: int) -> pd.DataFrame:
        """
        Normalizes CountTime to minutes for the pipeline.
        count_time_unit: 1=Minutes (no change), 2=Seconds (convert /60)
        """
        if 'CountTime' in df.columns and int(count_time_unit) == 2:
            df['CountTime'] = pd.to_numeric(df['CountTime'], errors='coerce').fillna(0.0) / 60.0
        return df


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
QDateTimeEdit::drop-down { background: #C5CAE9; border: none; width: 22px; border-radius: 0 6px 6px 0; }
QDateTimeEdit::drop-down:hover { background: #7986CB; }
QDateTimeEdit::drop-down:pressed { background: #3949AB; }
QDateTimeEdit::down-arrow { image: none; width: 0; height: 0; border-left: 5px solid transparent; border-right: 5px solid transparent; border-top: 6px solid #3949AB; }
"""

# Import the import dialog from refactored module
from .main_dialog import TrimsLSCImportDialog
from validation_gui import QCStatusWidget

class TrimsLSCDetailsWindow(QDialog):
    def __init__(self, run_id, parent=None, equipment_id=None):
        super().__init__(parent)
        self.run_id = run_id        
        self.equipment_id = equipment_id
        self.status_combos = []
        self.current_user = get_current_user_id()
        self.setWindowTitle(f'LSC Run {run_id}')
        screen = QDesktopWidget().screenGeometry(0)
        width, height = 1360, 940
        left = screen.left() + (screen.width() - width) // 2
        top = screen.top() + (screen.height() - height) // 2
        self.setGeometry(left, top, width, height)
        self.layout = QVBoxLayout(self)

        # Actions
        actions_top = QHBoxLayout(); actions_top.addStretch()
        self.btnEdit = QPushButton('Edit')
        self.btnSave = QPushButton('Save')
        self.btnCancel = QPushButton('Cancel')
        self.btnSave.hide()
        self.btnCancel.hide()
        self.btnEdit.clicked.connect(self._start_edit)
        self.btnSave.clicked.connect(self._save_edit)
        self.btnCancel.clicked.connect(self._cancel_edit)

        self.btnImport = QPushButton('Import'); self.btnImport.clicked.connect(self._open_import)
        self.btnPrint = QPushButton('Print Load List'); self.btnPrint.clicked.connect(self._print_loadlist)
        self.btnExport = QPushButton('Export Run Data'); self.btnExport.clicked.connect(self._export)
        self.btnQCReview = QPushButton('QC / Validate')
        self.btnQCReview.setStyleSheet(
            "QPushButton { background:#2D6A2D; color:white; font-weight:bold; padding:4px 12px; border-radius:3px; }"
            "QPushButton:hover { background:#3A8A3A; }"
            "QPushButton:disabled { background:#AAAAAA; color:#DDDDDD; }"
        )
        self.btnQCReview.clicked.connect(self._open_qc_tab)

        cls = QPushButton('Close'); cls.clicked.connect(self.accept)

        for b in [self.btnEdit, self.btnSave, self.btnCancel, self.btnImport, self.btnPrint, self.btnExport, self.btnQCReview, cls]:
            actions_top.addWidget(b)

        self.layout.addLayout(actions_top)

        self._init_metadata_section()
        self.tabs = QTabWidget(); self.layout.addWidget(self.tabs)
        self._init_tabs()
        # self._init_footer()

        # Populate Combos & Data
        self._populate_combos()
        self.refresh_all()

    def _init_metadata_section(self):
        self.setStyleSheet(META_STYLE)
        def lbl(text):
            l = QLabel(text); l.setProperty("class", "header-label"); l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return l

        # --- Run Metadata Group ---
        self.grpMeta = QGroupBox('Run Metadata & Staff')
        grid = QGridLayout(); grid.setContentsMargins(10,5,10,5); grid.setHorizontalSpacing(20); grid.setVerticalSpacing(4)

        # Widgets
        self.txtRunID = QLineEdit(str(self.run_id)); self.txtRunID.setReadOnly(True)
        self.cmbStatus = QComboBox(); self.cmbStatus.setEnabled(False)
        self.dtStart = QDateTimeEdit(); self.dtStart.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dtStart.setCalendarPopup(True); self.dtStart.setReadOnly(True)
        self.dtStart.setMinimumDateTime(QDateTime(1900, 1, 1, 0, 0))
        self.dtStart.setSpecialValueText("—")
        self.dtEnd = QDateTimeEdit(); self.dtEnd.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dtEnd.setCalendarPopup(True); self.dtEnd.setReadOnly(True)
        self.dtEnd.setMinimumDateTime(QDateTime(1900, 1, 1, 0, 0))
        self.dtEnd.setSpecialValueText("—")
        self.cmbProcedure = QComboBox(); self.cmbProcedure.setEnabled(False)
        self.txtEquipment = QLineEdit(); self.txtEquipment.setReadOnly(True)
        self.txtPath = QLineEdit(); self.txtPath.setReadOnly(True)
        self.txtRemarks = QLineEdit(); self.txtRemarks.setReadOnly(True)
        self.cmbTech1 = QComboBox(); self.cmbTech1.setEnabled(False)
        self.cmbTech2 = QComboBox(); self.cmbTech2.setEnabled(False)

        # Layout rows
        grid.addWidget(lbl('Run No.'), 0, 0); grid.addWidget(self.txtRunID, 0, 1)
        grid.addWidget(lbl('Status'), 0, 2); grid.addWidget(self.cmbStatus, 0, 3)
        grid.addWidget(lbl('Start time'), 1, 0); grid.addWidget(self.dtStart, 1, 1)
        grid.addWidget(lbl('End time'), 1, 2); grid.addWidget(self.dtEnd, 1, 3)
        grid.addWidget(lbl('Procedure'), 2, 0); grid.addWidget(self.cmbProcedure, 2, 1)
        grid.addWidget(lbl('Counter'), 2, 2); grid.addWidget(self.txtEquipment, 2, 3)
        grid.addWidget(lbl('Technician 1'), 3, 0); grid.addWidget(self.cmbTech1, 3, 1)
        grid.addWidget(lbl('Technician 2'), 3, 2); grid.addWidget(self.cmbTech2, 3, 3)
        grid.addWidget(lbl('Data Path'), 4, 0); grid.addWidget(self.txtPath, 4, 1, 1, 3)
        grid.addWidget(lbl('Remarks'), 5, 0); grid.addWidget(self.txtRemarks, 5, 1, 1, 3)
        self.grpMeta.setLayout(grid)

        # --- Computed Data Group ---
        self.grpComp = QGroupBox('Computed Data')
        cgrid = QGridLayout(); cgrid.setContentsMargins(10,5,10,5); cgrid.setHorizontalSpacing(20); cgrid.setVerticalSpacing(4)
        self.txtBkg = QLineEdit(); self.txtBkg.setReadOnly(True)
        self.txtBkgUnc = QLineEdit(); self.txtBkgUnc.setReadOnly(True)
        self.txtEff = QLineEdit(); self.txtEff.setReadOnly(True)
        self.txtEffUnc = QLineEdit(); self.txtEffUnc.setReadOnly(True)
        self.txtSpike = QLineEdit(); self.txtSpike.setReadOnly(True)
        self.txtSpikeUnc = QLineEdit(); self.txtSpikeUnc.setReadOnly(True)
        self.txtStd = QLineEdit(); self.txtStd.setReadOnly(True)
        self.txtStdUnc = QLineEdit(); self.txtStdUnc.setReadOnly(True)
        self.txtFOM = QLineEdit(); self.txtFOM.setReadOnly(True)
        self.txtMDA = QLineEdit(); self.txtMDA.setReadOnly(True)
        self.txtLC = QLineEdit(); self.txtLC.setReadOnly(True)
        self.txtLDL = QLineEdit(); self.txtLDL.setReadOnly(True)

        cgrid.addWidget(lbl('Mean Background'), 0, 0); cgrid.addWidget(self.txtBkg, 0, 1)
        cgrid.addWidget(lbl('±'), 0, 2); cgrid.addWidget(self.txtBkgUnc, 0, 3)
        cgrid.addWidget(lbl('Counter Efficiency'), 1, 0); cgrid.addWidget(self.txtEff, 1, 1)
        cgrid.addWidget(lbl('±'), 1, 2); cgrid.addWidget(self.txtEffUnc, 1, 3)
        cgrid.addWidget(lbl('Spike Activity'), 2, 0); cgrid.addWidget(self.txtSpike, 2, 1)
        cgrid.addWidget(lbl('±'), 2, 2); cgrid.addWidget(self.txtSpikeUnc, 2, 3)
        cgrid.addWidget(lbl('Standard Activity'), 3, 0); cgrid.addWidget(self.txtStd, 3, 1)
        cgrid.addWidget(lbl('±'), 3, 2); cgrid.addWidget(self.txtStdUnc, 3, 3)
        cgrid.addWidget(lbl('FOM [E²/B]'), 4, 0); cgrid.addWidget(self.txtFOM, 4, 1)
        cgrid.addWidget(lbl('MDA (DPM/Kg)'), 5, 0); cgrid.addWidget(self.txtMDA, 5, 1)
        cgrid.addWidget(lbl('LC'), 6, 0); cgrid.addWidget(self.txtLC, 6, 1)
        cgrid.addWidget(lbl('LLD'), 6, 2); cgrid.addWidget(self.txtLDL, 6, 3)
        # cgrid.addWidget(lbl('Used EF Method'), 7, 0); cgrid.addWidget(self.cmbEFMethod, 7, 1)
        # cgrid.addWidget(lbl('Unit'), 7, 2); cgrid.addWidget(self.cmbActivityUnit, 7, 3)
        self.grpComp.setLayout(cgrid)

        container = QWidget(); top_grid = QGridLayout(container)
        top_grid.setContentsMargins(6, 4, 6, 4)
        top_grid.setHorizontalSpacing(14); top_grid.setVerticalSpacing(6)
        top_grid.addWidget(self.grpMeta, 0, 0)
        top_grid.addWidget(self.grpComp, 0, 1)
        top_grid.setColumnStretch(0, 1)
        top_grid.setColumnStretch(1, 1)
        self.layout.addWidget(container)

    def _init_tabs(self):
        self.tabLL = QWidget(); l = QVBoxLayout(self.tabLL)
        self.tblLL = QTableView(); self.modLL = QStandardItemModel(); self.tblLL.setModel(self.modLL)
        l.addWidget(self.tblLL); self.tabs.addTab(self.tabLL, 'Load List')

        self.tabRes = QWidget(); l2 = QVBoxLayout(self.tabRes)
        self.tblRes = QTableView(); self.modRes = QStandardItemModel(); self.tblRes.setModel(self.modRes)
        l2.addWidget(self.tblRes); self.tabs.addTab(self.tabRes, 'Results')

        self._style_table(self.tblLL); self._style_table(self.tblRes)

        self.tabQC = QWidget(); l3 = QVBoxLayout(self.tabQC); l3.setContentsMargins(8, 8, 8, 8)
        self._qc_widget = QCStatusWidget(
            module         = "LSC",
            run_id         = self.run_id,
            parent_refresh = self.refresh_all,
            parent         = self.tabQC,
        )
        self._qc_widget.validated.connect(self.refresh_all)
        l3.addWidget(self._qc_widget)
        self.tabs.addTab(self.tabQC, 'QC / Validation')

    def _style_table(self, table):
        table.setShowGrid(False)
        table.setStyleSheet(TABLE_STYLE)
        table.setAlternatingRowColors(True)
        v_header = table.verticalHeader()
        v_header.setVisible(False)
        v_header.setSectionResizeMode(v_header.Fixed)
        v_header.setDefaultSectionSize(24)
        table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)

    # def _init_footer(self):
    #     h = QHBoxLayout()
    #     btn = QPushButton('Import Data'); btn.clicked.connect(self._open_import)
    #     h.addWidget(btn); h.addStretch()
        # cls = QPushButton('Close'); cls.clicked.connect(self.accept)
        # h.addWidget(cls); self.layout.addLayout(h)

    def _populate_combos(self):
        with db_manager.get_connection() as conn:
            # Status — positive codes first, Cancelled (-9) last
            self.cmbStatus.clear()
            rows = conn.execute(text("""
                SELECT StatusLookup.Status, StatusLookup.Description
                FROM StatusLookup
                WHERE StatusLookup.Status IN (6, 7, 8, 98, -9)
                ORDER BY CASE WHEN StatusLookup.Status < 0 THEN 9999 ELSE StatusLookup.Status END
            """)).fetchall()
            for r in rows:
                self.cmbStatus.addItem(r.Description, r.Status)

            # Procedures
            self.cmbProcedure.clear()
            rows = conn.execute(text("SELECT ProcedureID, ProcedureName FROM AnalysisProcedure ORDER BY ProcedureName")).fetchall()
            for r in rows:
                self.cmbProcedure.addItem(r.ProcedureName, r.ProcedureID)

            # Employees (Techs)
            self.cmbTech1.clear(); self.cmbTech2.clear()
            self.cmbTech1.addItem("", None); self.cmbTech2.addItem("", None)
            rows = conn.execute(text("SELECT EmployeeID, LastName, FirstMiddleName FROM Employee ORDER BY LastName")).fetchall()
            for r in rows:
                display = format_person_name(r.LastName, r.FirstMiddleName)
                self.cmbTech1.addItem(display, r.EmployeeID)
                self.cmbTech2.addItem(display, r.EmployeeID)

    def refresh_all(self):
        self._refresh_metadata(); self._refresh_loadlist(); self._refresh_results()
        self._check_finalized()

    def _check_finalized(self):
        """Update window title and edit-button state based on run lock status."""
        try:
            is_locked = False
            with db_manager.get_connection() as conn:
                run_row = conn.execute(text(
                    "SELECT IsLocked FROM TRIMS.LSCRun WHERE RunID = :rid"
                ), {'rid': self.run_id}).fetchone()
                is_locked = bool(getattr(run_row, 'IsLocked', False)) if run_row else False
            self.btnEdit.setEnabled(not is_locked)
            self.btnSave.hide()
            self.btnCancel.hide()
            self.btnEdit.show()
            self.btnImport.setText('Re-Import' if is_locked else 'Import')
            if is_locked:
                self.setWindowTitle(f'LSC Run {self.run_id}  [FINALIZED]')
        except Exception as e:
            logging.warning(f"_check_finalized failed: {e}")

    def _open_qc_tab(self):
        """Switch to the QC / Validation tab and refresh the widget."""
        self._qc_widget.refresh()
        self.tabs.setCurrentIndex(2)


    def _refresh_metadata(self):
        with db_manager.get_connection() as conn:
            run = conn.execute(text('SELECT * FROM TRIMS.LSCRun WHERE RunID=:id'), {'id': self.run_id}).fetchone()
            if not run:
                return

            # --- Basic run info ---
            for _attr, _w in [('RunStartTime', self.dtStart), ('RunEndTime', self.dtEnd)]:
                _v = getattr(run, _attr, None)
                if _v and hasattr(_v, 'year'):
                    _w.setDateTime(QDateTime(_v.year, _v.month, _v.day, _v.hour, _v.minute))
                else:
                    _w.setDateTime(_w.minimumDateTime())
            self.txtPath.setText(str(getattr(run, 'DataPath', '') or ''))
            self.txtRemarks.setText(str(getattr(run, 'Remarks', '') or ''))

            # Combos
            idx = self.cmbStatus.findData(getattr(run, 'RunStatus', None))
            if idx >= 0: self.cmbStatus.setCurrentIndex(idx)
            idx = self.cmbProcedure.findData(getattr(run, 'ProcedureID', None))
            if idx >= 0: self.cmbProcedure.setCurrentIndex(idx)
            idx = self.cmbTech1.findData(getattr(run, 'TechnicianID', None))
            if idx >= 0: self.cmbTech1.setCurrentIndex(idx)
            else: self.cmbTech1.setCurrentIndex(0)
            idx = self.cmbTech2.findData(getattr(run, 'Technician2', None))
            if idx >= 0: self.cmbTech2.setCurrentIndex(idx)
            else: self.cmbTech2.setCurrentIndex(0)

            # Equipment
            try:
                eq = conn.execute(text('SELECT EquipmentName FROM Equipment WHERE EquipmentID=:e'),
                                {'e': getattr(run, 'EquipmentID', None)}).fetchone()
                self.txtEquipment.setText(eq.EquipmentName if eq else str(getattr(run, 'EquipmentID', '')))
            except Exception:
                self.txtEquipment.setText(str(getattr(run, 'EquipmentID', '')))

            # --- Computed run params (bkg & efficiency) already saved on Step-3 ---
            self.txtBkg.setText(self._fmt(getattr(run, 'MeanBackground', None), 2))
            self.txtBkgUnc.setText(self._fmt(getattr(run, 'MeanBackgroundUnc', None), 2))
            self.txtEff.setText(self._fmt(getattr(run, 'CounterEfficiency', None), 4))
            self.txtEffUnc.setText(self._fmt(getattr(run, 'CounterEfficiencyUnc', None), 4))

            # --- Helper: weighted average with 1/σ²; falls back to simple mean ---
            def _wavg(values, uncs):
                vals = [float(v) for v in values if v is not None]
                sigs = [float(u) for u in uncs if u is not None and float(u) > 0]
                if not vals:
                    return None, None
                if len(sigs) == len(vals) and len(vals) > 0:
                    w = [1.0/(u*u) for u in sigs]
                    vbar = sum(v*w_i for v, w_i in zip(vals, w)) / sum(w)
                    ubar = (1.0/sum(w))**0.5
                    return vbar, ubar
                # fallback: simple mean, unc = stdev/sqrt(n) if available
                vbar = sum(vals)/len(vals)
                if len(vals) > 1:
                    s = (sum((v - vbar)**2 for v in vals)/(len(vals)-1))**0.5
                    ubar = s/(len(vals)**0.5)
                else:
                    ubar = 0.0
                return vbar, ubar

            # --- Pull list so we can find spike/standard vials ---
            run_date = getattr(run, 'RunStartTime', None)
            if isinstance(run_date, str):
                try: run_date = datetime.fromisoformat(run_date)
                except: run_date = None

            with db_manager.get_engine().connect() as _sa_conn:
                df_ll = pd.read_sql(text("""
                    SELECT ll.PositionInRun, ll.SampleType, a.SampleID, a.Prefix
                    FROM TRIMS.LSCLoadList ll
                    JOIN Analysis a ON ll.AnalysisID = a.AnalysisID
                    WHERE ll.RunID = :rid
                    ORDER BY ll.PositionInRun
                """), _sa_conn, params={'rid': self.run_id})

            # --- Standards (SampleType=2) and Spikes (SampleType=3)
            #     -> display TU (non-decay-corrected) like the original
            std_vals, std_uncs = [], []
            spk_vals, spk_uncs = [], []

            # Normalise column names to lowercase so both PostgreSQL and SQL Server work
            df_ll.columns = [c.lower() for c in df_ll.columns]

            for _, row in df_ll.iterrows():
                sid = row.get('sampleid', None)
                if not sid:
                    continue
                try:
                    if int(row['sampletype']) == STYPE_LSC_STD:
                        v, u, _, _ = get_standard_activity(conn, sid, run_date, "TU", measurable_id=3, DecayCorrected=False)
                        if v is not None:
                            std_vals.append(v); std_uncs.append(u if u is not None else 0.0)
                    elif int(row['sampletype']) == STYPE_LSC_SPIKE_ENR:
                        v, u, _, _ = get_standard_activity(conn, sid, run_date, "TU", measurable_id=3, DecayCorrected=False)
                        if v is not None:
                            spk_vals.append(v); spk_uncs.append(u if u is not None else 0.0)
                except Exception:
                    # ignore bad rows and keep going
                    pass

            std_mean, std_unc = _wavg(std_vals, std_uncs)
            spk_mean, spk_unc = _wavg(spk_vals, spk_uncs)

            # Write to UI (blank if none)
            self.txtStd.setText(self._fmt(std_mean, 2))
            self.txtStdUnc.setText(self._fmt(std_unc, 2))
            self.txtSpike.setText(self._fmt(spk_mean, 2))
            self.txtSpikeUnc.setText(self._fmt(spk_unc, 2))

            # --- Detection Limits (run-level display) ---
            try:
                # 1. Ensure we are passing a fresh connection
                res = compute_detection_limit_from_run(conn, run_id=self.run_id)
                
                # 2. Format to 3 decimal places using your helper
                # If the DB has LLD/LC saved, we can also fallback to those
                mda_val = res.get('MDA') if res.get('MDA') else getattr(run, 'LLD', 0)
                lc_val = res.get('LC') if res.get('LC') else getattr(run, 'LC', 0)
                
                self.txtMDA.setText(self._fmt(mda_val, 3))
                self.txtLC.setText(self._fmt(lc_val, 3))
                
                # 3. FOM Calculation
                bkg_val = float(getattr(run, 'MeanBackground', 0) or 0)
                eff_val = float(getattr(run, 'CounterEfficiency', 0) or 0)
                
                if bkg_val > 0 and eff_val > 0:
                    # E is usually a fraction (0.453), FOM uses E as percentage
                    e_pct = eff_val * 100 if eff_val <= 1.0 else eff_val
                    fom = (e_pct ** 2) / bkg_val
                    self.txtFOM.setText(self._fmt(fom, 2))
            except Exception as e:
                logging.error(f"Detection limit refresh failed: {e}")
                self.txtMDA.setText("0.000")
                self.txtLC.setText("0.000")

    def _fmt(self, val, nd):
        try:
            if val is None: return ''
            return f"{float(val):.{nd}f}"
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return str(val)

    def _refresh_loadlist(self):
        """Load List with per‑vial LC/MDA (EF-scaled) and formatted values."""
        self.modLL.clear()
        headers = [
            'Tray', 'Order', 'SampleID', 'Sample Name', 'Amount', 'Diluent',
            'Net CPM', 'Ignore', 'Remarks'
        ]
        self.modLL.setHorizontalHeaderLabels(headers)

        # Make table read-only by default
        self.tblLL.setEditTriggers(QTableView.NoEditTriggers)
        
        with db_manager.get_connection() as conn:
            # Run-level parameters
            run_data = conn.execute(text("""
                SELECT MeanBackground, CounterEfficiency, CounterEfficiencyUnc
                FROM TRIMS.LSCRun WHERE RunID = :rid
            """), {'rid': self.run_id}).fetchone()
            if not run_data:
                return
            bkg_cpm = float(run_data.MeanBackground or 0.0)
            eff_raw = float(run_data.CounterEfficiency or 0.0)
            eff_unc = float(run_data.CounterEfficiencyUnc or 0.0)
            eff_frac = eff_raw if eff_raw <= 1.0 else eff_raw / 100.0
            eff_unc_frac = eff_unc if eff_raw <= 1.0 else eff_unc / 100.0

            # total background time (minutes)
            bkg_time_sum = float(conn.execute(text("""
                SELECT COALESCE(SUM(CountTime), 0)
                FROM TRIMS.LSCLoadList
                WHERE RunID = :rid AND SampleType = 1
            """), {'rid': self.run_id}).scalar() or 0.0)
            if bkg_time_sum <= 0:
                bkg_time_sum = 1.0

            # main data from view
            _VW_RENAME = {
                'runid':'RunID','countid':'CountID','analysisid':'AnalysisID',
                'sampleid':'SampleID','prefix':'Prefix','sname':'sName',
                'sampletype':'SampleType','sampleamount':'SampleAmount',
                'samplediluent':'SampleDiluent','positioninrun':'PositionInRun',
                'traynumber':'TrayNumber','positionintray':'PositionInTray',
                'counttime':'CountTime','result':'Result','resultunc':'ResultUnc',
                'isignored':'IsIgnored','remarks':'Remarks','status':'Status',
                'meanvalue':'MeanValue','meanvalueunc':'MeanValueUnc',
                'enrichmentfactor':'EnrichmentFactor','enrichmentfactorunc':'EnrichmentFactorUnc',
                'finalactivity':'FinalActivity','finalactivityunc':'FinalActivityUnc',
                'activityunit':'ActivityUnit','rejectflag':'RejectFlag','lldstatus':'LLDStatus',
            }
            with db_manager.get_engine().connect() as _sa_conn:
                df = pd.read_sql(text("""
                    SELECT * FROM vwlscloadlist
                    WHERE runid = :id
                    ORDER BY positioninrun
                """), _sa_conn, params={'id': self.run_id})
            df = df.rename(columns=_VW_RENAME)

            required_cols = [
                'TrayNumber', 'PositionInTray', 'PositionInRun','AnalysisID', 'SampleID', 'sName', 'SampleAmount',
                'SampleDiluent', 'Result', 'ResultUnc', 'MeanValue', 'MeanValueUnc',
                'EnrichmentFactor', 'EnrichmentFactorUnc', 'FinalActivity',
                'FinalActivityUnc', 'IsIgnored', 'Remarks', 'Prefix', 'CountTime',
                'ActivityUnit'
            ]
            for c in required_cols:
                if c not in df.columns:
                    df[c] = None

            def fmt_pm(v, u, nd=2):
                v_ok = (v is not None) and (not pd.isnull(v))
                u_ok = (u is not None) and (not pd.isnull(u))
                if v_ok and u_ok: return f"{float(v):.{nd}f} ± {float(u):.{nd}f}"
                elif v_ok: return f"{float(v):.{nd}f}"
                return ''

            for _, row in df.iterrows():
                tn  = row['TrayNumber']
                pit = row['PositionInTray']
                if pd.notnull(tn) and pd.notnull(pit):
                    tray = f"{int(tn)}-{int(pit)}"
                elif pd.notnull(tn):
                    tray = str(int(tn))
                else:
                    tray = ''
                order = '' if pd.isnull(row['PositionInRun']) else str(int(row['PositionInRun']))
                prefix = f"{str(row['Prefix'])}-" if not pd.isnull(row['Prefix']) else ""
                sid = prefix + (str(int(row['SampleID'])) if not pd.isnull(row['SampleID']) else '')
                sname = '' if pd.isnull(row['sName']) else str(row['sName'])
                amt_g = float(row['SampleAmount'] or 0.0) if pd.notnull(row['SampleAmount']) else 0.0
                dil_g = float(row['SampleDiluent'] or 0.0) if pd.notnull(row['SampleDiluent']) else 0.0
                vial_mass_kg = max((amt_g - dil_g), 0.1) / 1000.0
                vial_time = float(row['CountTime'] or 1.0)
                ef_val = float(row['EnrichmentFactor'] or 1.0)
                netcpm = fmt_pm(row['MeanValue'], row['MeanValueUnc'])
                netdpm = fmt_pm(row['Result'], row['ResultUnc'])
                ef = fmt_pm(row['EnrichmentFactor'], row['EnrichmentFactorUnc'])
                fact = fmt_pm(row['FinalActivity'], row['FinalActivityUnc'])
                ignore = bool(row['IsIgnored']) if row['IsIgnored'] is not None and not pd.isnull(row['IsIgnored']) else False
                remarks= '' if pd.isnull(row['Remarks']) else str(row['Remarks'])
                analysis_id = int(row['AnalysisID']) if pd.notnull(row['AnalysisID']) else None

                items = [
                    QStandardItem(tray),
                    QStandardItem(order),
                    QStandardItem(sid),
                    QStandardItem(sname),
                    QStandardItem(f"{amt_g:.1f}"),
                    QStandardItem(f"{dil_g:.2f}"),
                    QStandardItem(netcpm),
                    QStandardItem(''),
                    QStandardItem(remarks)
                ]
                items[7].setCheckable(True)
                items[7].setCheckState(Qt.Checked if ignore else Qt.Unchecked)
                
                for i in [4, 5, 8]:  # Amount, Diluent, Remarks
                    items[i].setEditable(False)
                
                self.modLL.appendRow(items)
                
            self.tblLL.resizeColumnsToContents()
            self.tblLL.setColumnWidth(8, 250)
            
    def _perform_statistical_validation(self, current_stds_df, hist_eff):
        """
        Performs Chi-squared test and individual vial validation.
        """
        # 1. Chi-Squared Test for Standards
        # Formula: sum((Observed_i - Expected_i)^2 / Expected_i)
        # Expected = True_DPM * Efficiency
        x_true = current_stds_df['TrueDPM'].values
        y_obs = current_stds_df['NetCPM'].values
        y_exp = x_true * self.eff
        
        chi_sq = np.sum(((y_obs - y_exp)**2) / (y_exp + 1e-12))
        df_degrees = len(x_true) - 1
        
        # Simple threshold check (e.g., alpha=0.05)
        if df_degrees > 0 and chi_sq > 11.07: # Critical value for df=5, alpha=0.05
            QMessageBox.warning(self, "Chi-Squared Warning", 
                f"Chi-squared ({chi_sq:.2f}) exceeds threshold. The fit to the "
                "calibration curve is statistically poor.")

    def _validate_vial_results(self, row, lc, mda):
        """
        Categorizes vial activity relative to detection limits.
        """
        activity = float(row.FinalActivity or 0.0)
        if activity < lc:
            return "Below LC (Not Detected)"
        elif activity < mda:
            return "Above LC, Below MDA (Qualitative)"
        else:
            return "Quantifiable"
        
    def _refresh_results(self):
        """Primary results view: relation between raw Net CPM and Final Environmental Activity."""
        self.modRes.clear()
        self.status_combos = []
        
        headers = ['Prefix-SampleID', 'AnalysisID',
                   'Net CPM', 'DPM/Kg',
                   'EF ± Unc', 'Enr. Method',
                   'Final Activity ± Unc', 'Unit', 'LC', 'MDA', 'Acceptance', 'Status']
        self.modRes.setHorizontalHeaderLabels(headers)

        status_options = self._get_status_options()
        
        with db_manager.get_connection() as conn:
            run = conn.execute(text(f"SELECT {db_manager.sql_top(1)}MeanBackground, CounterEfficiency FROM TRIMS.LSCRun WHERE RunID=:id{db_manager.sql_limit(1)}"),
                               {'id': self.run_id}).fetchone()
            bkg_cpm = float(run.MeanBackground or 0.0)
            eff_frac = float(run.CounterEfficiency or 1.0)
            if eff_frac > 1.0: eff_frac /= 100.0

            bkg_time_sum = float(conn.execute(text(
                "SELECT COALESCE(SUM(CountTime), 0) FROM TRIMS.LSCLoadList WHERE RunID=:id AND SampleType=1"
            ), {'id': self.run_id}).scalar() or 1.0)

            sql = """
                SELECT a.AnalysisID, a.Prefix, a.SampleID, a.Status AS AnalysisStatus,
                       rm.MeanValue, rm.MeanValueUnc,
                       ll.Result      AS DPM_kg,
                       ll.ResultUnc   AS DPM_kg_Unc,
                       ll.SampleAmount, ll.SampleDiluent, ll.CountTime,
                       lr.EnrichmentFactor, lr.EnrichmentFactorUnc,
                       mu.ShortName   AS UnitName,
                       lr.ActivityUnit,
                       COALESCE(m.sName, 'Spike') AS MethodName,
                       lr.FinalActivity, lr.FinalActivityUnc, lr.RejectFlag, ll.Status AS LLstatus, lr.lldstatus
                FROM TRIMS.LSCLoadList ll
                JOIN Analysis a                ON ll.AnalysisID = a.AnalysisID
                LEFT JOIN TRIMS.LSCRunMean rm  ON rm.CountID = ll.CountID
                                              AND rm.ValueKind = 1
                LEFT JOIN TRIMS.LSCResult lr   ON lr.RunID = ll.RunID AND lr.AnalysisID = ll.AnalysisID
                LEFT JOIN MeasurementUnit mu   ON lr.ActivityUnit = mu.UnitID
                LEFT JOIN GuitblEnrichmentFactorMethod m ON lr.EnrichmentFactorMethod = m.ID
                WHERE ll.RunID = :id
                ORDER BY ll.PositionInRun
            """
            rows = conn.execute(text(sql), {'id': self.run_id}).fetchall()

            for r in rows:
                psid = f"{(r.Prefix or '').strip()}-{int(r.SampleID) if r.SampleID else ''}".strip('-')
                aid  = str(int(r.AnalysisID)) if r.AnalysisID else ''

                # Net CPM (raw)
                if r.MeanValue is not None and r.MeanValueUnc is not None:
                    net_cpm_disp = f"{float(r.MeanValue):.2f} ± {float(r.MeanValueUnc):.2f}"
                elif r.MeanValue is not None:
                    net_cpm_disp = f"{float(r.MeanValue):.2f}"
                else:
                    net_cpm_disp = ''

                # Stable DPM/kg
                if r.DPM_kg is not None and r.DPM_kg_Unc is not None:
                    dpmkg_disp = f"{float(r.DPM_kg):.2f} ± {float(r.DPM_kg_Unc):.2f}"
                elif r.DPM_kg is not None:
                    dpmkg_disp = f"{float(r.DPM_kg):.2f}"
                else:
                    dpmkg_disp = ''

                # EF & method
                ef_disp = ''
                if r.EnrichmentFactor is not None and r.EnrichmentFactorUnc is not None:
                    ef_disp = f"{float(r.EnrichmentFactor):.3f} ± {float(r.EnrichmentFactorUnc):.3f}"
                elif r.EnrichmentFactor is not None:
                    ef_disp = f"{float(r.EnrichmentFactor):.3f}"
                method_disp = r.MethodName or 'Spike'

                # Final Activity
                fa_disp = ''
                if r.FinalActivity is not None and r.FinalActivityUnc is not None:
                    fa_disp = f"{float(r.FinalActivity):.2f} ± {float(r.FinalActivityUnc):.2f}"
                elif r.FinalActivity is not None:
                    fa_disp = f"{float(r.FinalActivity):.2f}"
                unit_disp = r.UnitName or ''

                # LC/MDA per vial
                vial_mass_kg = max((float(r.SampleAmount or 0) - float(r.SampleDiluent or 0)), 0.1) / 1000.0
                ld = lower_detection_limit(
                    bkg_cpm=float(bkg_cpm),
                    bkg_time_min=float(bkg_time_sum),
                    sample_time_min=float(r.CountTime or 1.0),
                    eff_frac=float(eff_frac),
                    vol_kg=vial_mass_kg
                )
                ef_val = float(getattr(r, 'EnrichmentFactor', 1.0) or 1.0)
                lc_u, _  = convert_activity_unit(2, int(r.ActivityUnit or 1), (ld['LC_dpm_per_kg'] / ef_val), 0, volume=float(r.SampleAmount or 0))
                mda_u, _ = convert_activity_unit(2, int(r.ActivityUnit or 1), (ld['LD_dpm_per_kg'] / ef_val), 0, volume=float(r.SampleAmount or 0))            

                fa_str = f"{r.FinalActivity:.2f}" if r.FinalActivity is not None else ""
                fa_unc_str = f"{r.FinalActivityUnc:.2f}" if r.FinalActivityUnc is not None else ""
                val_item = QStandardItem(f"{fa_str} ± {fa_unc_str}")
            
                # Determine acceptance and color coding based on lldstatus or calculated values
                if getattr(r, 'lldstatus', None) is not None:
                    lds = int(r.lldstatus)
                    if lds == 0:
                        acceptance_str = "Quantifiable"
                        color = QColor("#C8E6C9") # Light Green
                    elif lds == 1:
                        acceptance_str = "Below LC (Not Detected)"
                        color = Qt.lightGray
                    elif lds == 2:
                        acceptance_str = "Above LC, Below MDA (Qualitative)"
                        color = QColor("#FFF9C4") # Light Yellow
                    else:
                        acceptance_str = f"Unknown ({lds})"
                        color = Qt.lightGray
                else:
                    acceptance_str = self._validate_vial_results(r, lc_u, mda_u)
                    if r.FinalActivity is not None:
                        if r.FinalActivity < lc_u:
                            color = Qt.lightGray
                        elif r.FinalActivity < mda_u:
                            color = QColor("#FFF9C4") # Light Yellow
                        else:
                            color = QColor("#C8E6C9") # Light Green
                    else:
                        color = None

                acceptance_item = QStandardItem(acceptance_str)
                if color is not None:
                    for item in [val_item, acceptance_item]:
                        item.setBackground(color)

                # Status combobox
                status_combo = QComboBox()
                for status_id, status_desc in status_options:
                    status_combo.addItem(status_desc, status_id)
                # Set current status (default to RunStatus if not set)
                current_status = r.LLstatus if r.LLstatus else self.run_status_id
                status_combo.setCurrentIndex(
                    status_combo.findData(current_status)
                )                
                # Store AnalysisID for UPDATE
                status_combo.setProperty('analysis_id', r.AnalysisID)                
                # Disable until Edit mode
                status_combo.setEnabled(False)                
                # Store reference for edit mode toggling
                if not hasattr(self, 'status_combos'):
                    self.status_combos = []
                self.status_combos.append(status_combo)
                
                # Ignore Toggle
                ignore_item = QStandardItem("")
                ignore_item.setCheckable(True)
                ignore_item.setCheckState(Qt.Checked if r.RejectFlag else Qt.Unchecked)
                ignore_item.setData(r.AnalysisID, Qt.UserRole) # Store ID for update            
                items =[
                    QStandardItem(psid),
                    QStandardItem(aid),
                    QStandardItem(net_cpm_disp),
                    QStandardItem(dpmkg_disp),
                    QStandardItem(ef_disp),
                    QStandardItem(method_disp),
                    QStandardItem(fa_disp),
                    QStandardItem(unit_disp),
                    QStandardItem(f"{lc_u:.2f}"),
                    QStandardItem(f"{mda_u:.2f}"),
                    acceptance_item,
                    QStandardItem('')  # Placeholder for combobox
                ]

                self.modRes.appendRow(items)
                
                # Set combobox widget in table
                self.tblRes.setIndexWidget(
                    self.modRes.index(self.modRes.rowCount()-1, 11),  # Status column
                    status_combo
                )
        self.tblRes.resizeColumnsToContents()

    def _get_status_options(self):
        """Get status options for combobox"""
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT StatusLookup.Status, StatusLookup.Description
                    FROM StatusLookup 
                    WHERE StatusLookup.Status IN (6, 7, 8, 98, -9)
                    ORDER BY CASE WHEN StatusLookup.Status < 0 THEN 9999 ELSE StatusLookup.Status END
                """)).fetchall()
                return [(r.Status, r.Description) for r in rows]
        except Exception as e:
            logging.error(f"Failed to load status options: {e}")

    def _save_results_status(self):
        """Save Status changes from Results table"""
        try:
            with db_manager.get_connection() as conn:
                for combo in getattr(self, 'status_combos', []):
                    analysis_id = combo.property('analysis_id')
                    status = combo.currentData()

                    if analysis_id and status:
                        conn.execute(text("""
                            UPDATE Analysis
                            SET Status = :status
                            WHERE AnalysisID = :aid
                        """), {
                            'status': status,
                            'aid': analysis_id
                        })
                conn.commit()
                logging.info(f"Saved status updates for run {self.run_id}")
        except Exception as e:
            logging.error(f'Status save failed: {e}')
            QMessageBox.warning(self, 'Save failed', f'Could not save status changes: {e}')
            
    def _toggle_edit_widgets(self, checked):
        self.txtPath.setReadOnly(not checked)
        self.txtRemarks.setReadOnly(not checked)
        self.cmbStatus.setEnabled(checked)
        self.cmbProcedure.setEnabled(checked)
        self.cmbTech1.setEnabled(checked)
        self.cmbTech2.setEnabled(checked)

    def _start_edit(self):
        self.btnEdit.hide()
        self.btnSave.show()
        self.btnCancel.show()
        self._toggle_edit(True)

    def _save_edit(self):
        self.btnSave.hide()
        self.btnCancel.hide()
        self.btnEdit.show()
        self._toggle_edit(False)

    def _cancel_edit(self):
        self.btnSave.hide()
        self.btnCancel.hide()
        self.btnEdit.show()
        # View Mode locking logic
        self.tblLL.setEditTriggers(QTableView.NoEditTriggers)
        for row in range(self.modLL.rowCount()):
            for col in [4, 5, 8]:
                item = self.modLL.item(row, col)
                if item:
                    item.setEditable(False)
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        for combo in getattr(self, 'status_combos', []):
            try:
                combo.setEnabled(False)
            except RuntimeError:
                pass
        self._toggle_edit_widgets(False)
        self.refresh_all()

    def _toggle_edit(self, checked):
        self._toggle_edit_widgets(checked)
        
        # Toggle Load List editable columns
        if checked:
            # EDIT MODE
            self.tblLL.setEditTriggers(
                QTableView.DoubleClicked | QTableView.EditKeyPressed
            )
            for row in range(self.modLL.rowCount()):
                for col in [4, 5, 8]:
                    item = self.modLL.item(row, col)
                    if item:
                        item.setEditable(True)
                        item.setFlags(item.flags() | Qt.ItemIsEditable)
            
            # Enable status combos (with safety)
            for combo in getattr(self, 'status_combos', []):
                try:
                    combo.setEnabled(True)
                except RuntimeError:
                    pass
        else:
            # VIEW MODE
            self.tblLL.setEditTriggers(QTableView.NoEditTriggers)
            for row in range(self.modLL.rowCount()):
                for col in [4, 5, 8]:
                    item = self.modLL.item(row, col)
                    if item:
                        item.setEditable(False)
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            
            # STOP EDIT - Save then refresh
            self._save_metadata()
            self._save_loadlist_edits()
            self._save_results_status()
            self.refresh_all()  # Recreates all widgets

    def _save_metadata(self):
        """Save run metadata"""
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE TRIMS.LSCRun SET
                    DataPath=:p,
                    Remarks=:r,
                    ProcedureID=:pid,
                    TechnicianID=:t1,
                    Technician2=:t2,
                    RunStatus=:st
                    WHERE RunID=:rid
                """), {
                    'p': self.txtPath.text().strip(),
                    'r': self.txtRemarks.text().strip(),
                    'pid': self.cmbProcedure.currentData(),
                    't1': self.cmbTech1.currentData(),
                    't2': self.cmbTech2.currentData(),
                    'st': self.cmbStatus.currentData(),
                    'rid': self.run_id
                })
                conn.commit()

            # Recompute LC/MDA
            with db_manager.get_connection() as conn:
                run = conn.execute(text('SELECT * FROM TRIMS.LSCRun WHERE RunID=:id'), 
                                {'id': self.run_id}).fetchone()
                if run:
                    res = compute_detection_limit_from_run(conn, run_id=self.run_id)
                    self.txtLC.setText(f"{res['LC']:.2f}")
                    self.txtMDA.setText(f"{res['MDA']:.2f}")
        except Exception as e:
            logging.error(f'Metadata save failed: {e}')
            QMessageBox.warning(self, 'Save failed', f'Could not save metadata: {e}')
            
    def _save_loadlist_edits(self):
        """Save edited Amount, Diluent, Ignore, Remarks from Load List"""
        # Check user has required role        
        if not check_employee_privilege(self.current_user, "AccessEvaluation"):  # Admin, Manager, Analyst roles
            QMessageBox.warning(self, "Permission Denied", 
                            "You don't have permission to edit load list data.")
            return
        
        try:
            with db_manager.get_connection() as conn:
                for row in range(self.modLL.rowCount()):
                    # Get AnalysisID from first item
                    analysis_id = self.modLL.item(row, 0).data(Qt.UserRole)
                    if not analysis_id:
                        continue
                    
                    # Get edited values
                    amount_item = self.modLL.item(row, 4)
                    diluent_item = self.modLL.item(row, 5)
                    ignore_item = self.modLL.item(row, 7)
                    remarks_item = self.modLL.item(row, 8)
                    
                    amount = float(amount_item.text()) if amount_item else 0.0
                    diluent = float(diluent_item.text()) if diluent_item else 0.0
                    ignore = (ignore_item.checkState() == Qt.Checked) if ignore_item else False
                    remarks = remarks_item.text() if remarks_item else ''
                    
                    # UPDATE database
                    conn.execute(text("""
                        UPDATE TRIMS.LSCLoadList
                        SET SampleAmount = :amt,
                            SampleDiluent = :dil,
                            IsIgnored = :ign,
                            Remarks = :rem
                        WHERE RunID = :rid 
                        AND AnalysisID = :aid
                    """), {
                        'amt': amount,
                        'dil': diluent,
                        'ign': ignore,
                        'rem': remarks,
                        'rid': self.run_id,
                        'aid': analysis_id
                    })
                conn.commit()
                logging.info(f"Saved load list edits for run {self.run_id}")
        except Exception as e:
            logging.error(f'Load list save failed: {e}')
            QMessageBox.warning(self, 'Save failed', f'Could not save load list edits: {e}')

    def _fetch_loadlist_specs(self):
        """One LabelSpec per unique vial (TrayNumber, PositionInTray), no duplicates.
        Includes all sample types so controls and standards appear in the tray grid.
        Returns (specs, type_map) where type_map: {(tn, pit): sampletype int}."""
        with db_manager.get_engine().connect() as sa_conn:
            df = pd.read_sql(text("""
                SELECT DISTINCT ON (ll.traynumber, ll.positionintray)
                       ll.traynumber     AS "TrayNumber",
                       ll.positionintray AS "PositionInTray",
                       ll.sampletype     AS "SampleType",
                       s.prefix          AS "Prefix",
                       s.sampleid        AS "SampleID",
                       ll.analysisid     AS "AnalysisID",
                       ll.positioninrun  AS "PositionInRun",
                       s.sname           AS "SName"
                FROM TRIMS.LSCLoadList ll
                JOIN Analysis a ON a.analysisid = ll.analysisid
                JOIN Sample   s ON s.sampleid = a.sampleid AND s.prefix = a.prefix
                WHERE ll.runid = :rid
                  AND ll.traynumber    IS NOT NULL
                  AND ll.positionintray IS NOT NULL
                ORDER BY ll.traynumber, ll.positionintray, ll.positioninrun
            """), sa_conn, params={'rid': self.run_id})

        specs    = []
        type_map = {}   # {(tn, pit): sampletype}
        for _, row in df.iterrows():
            tn  = row['TrayNumber']
            pit = row['PositionInTray']
            if pd.isnull(tn) or pd.isnull(pit):
                continue
            tn_i, pit_i = int(tn), int(pit)
            vial = f"{tn_i}-{pit_i}"
            st   = int(row['SampleType']) if pd.notnull(row['SampleType']) else 0
            type_map[(tn_i, pit_i)] = st
            specs.append(analysis_spec(
                prefix=str(row['Prefix'] or ""),
                sample_id=str(int(row['SampleID'])) if pd.notnull(row['SampleID']) else "",
                analysis_id=str(int(row['AnalysisID'])) if pd.notnull(row['AnalysisID']) else "—",
                container_num=vial,
                job_type="LSC",
                sample_name=str(row['SName']) if pd.notnull(row.get('SName')) else "",
            ))
        return specs, type_map

    def _fetch_tray_config(self):
        """Return (traycount, trayrows, traycols) for this run's instrument, or None."""
        try:
            with db_manager.get_engine().connect() as sa_conn:
                row = sa_conn.execute(text("""
                    SELECT tc.traycount, tc.trayrows, tc.traycols
                    FROM public.equipmenttrayconfig tc
                    JOIN trims.lscrun r ON r.equipmentid = tc.equipmentid
                    WHERE r.runid = :rid
                """), {'rid': self.run_id}).fetchone()
            return (row.traycount, row.trayrows, row.traycols) if row else None
        except Exception as exc:
            logging.warning("Could not fetch tray config: %s", exc)
            return None

    def _draw_tray_cell(self, *args, **kwargs):
        """Delegates to shared tray_print_renderer.draw_tray_cell."""
        _shared_draw_tray_cell(*args, **kwargs)

    def _render_tray_loadlist(self, printer: QPrinter, specs, tray_cfg, show_qr: bool, type_map=None) -> int:
        """Delegates to shared tray_print_renderer.render_tray_loadlist."""
        return _shared_render_tray(
            printer, specs, tray_cfg, show_qr,
            type_map=type_map,
            run_id=self.run_id,
            module_name="LSC",
            equip_name=self.txtEquipment.text() if hasattr(self, 'txtEquipment') else '',
            start_str=(self.dtStart.dateTime().toString("yyyy-MM-dd HH:mm")
                       if hasattr(self, 'dtStart') and self.dtStart.dateTime() != self.dtStart.minimumDateTime()
                       else ''),
        )

    def _render_loadlist(self, printer: QPrinter, specs, show_qr: bool = True) -> int:
        """Paint card grid onto printer; returns total page count."""
        dpi   = printer.resolution()
        mm2px = dpi / 25.4

        n_cols    = _LL_COLS        if show_qr else _LL_COLS_NOQR
        card_w_mm = _LL_CARD_W      if show_qr else _LL_CARD_W_NOQR
        card_h_mm = _LL_CARD_H      if show_qr else _LL_CARD_H_NOQR

        _s        = _AppSettings("YourOrg", "ISOTOPES_SUITE")
        _mg_mm    = float(_s.value("print/margin", 10.0))
        paper_mm  = printer.paperSize(QPrinter.Millimeter)
        page_w_px = paper_mm.width()  * mm2px
        page_h_px = paper_mm.height() * mm2px
        card_w_px = card_w_mm * mm2px
        card_h_px = card_h_mm * mm2px
        gap_h_px  = _LL_GAP_H * mm2px
        gap_v_px  = _LL_GAP_V * mm2px
        margin_l  = _mg_mm    * mm2px
        margin_t  = _mg_mm    * mm2px
        hdr_h_px  = _LL_HDR_H * mm2px

        avail_h  = page_h_px - margin_t - hdr_h_px
        rows_pp  = max(1, int((avail_h + gap_v_px) / (card_h_px + gap_v_px)))
        cards_pp = n_cols * rows_pp
        total_pg = (len(specs) + cards_pp - 1) // cards_pp

        mode_tag  = "Labels" if show_qr else "Load List"
        run_start = (self.dtStart.dateTime().toString("yyyy-MM-dd HH:mm")
                     if hasattr(self, 'dtStart') and self.dtStart.dateTime() != self.dtStart.minimumDateTime()
                     else '')
        run_equip = self.txtEquipment.text() if hasattr(self, 'txtEquipment') else ''
        run_label = (
            f"LSC Run {self.run_id}  ·  {mode_tag}   "
            f"Start: {run_start or '—'}   Equipment: {run_equip or '—'}"
        )

        painter = QPainter(printer)
        try:
            for page_idx, page_start in enumerate(range(0, len(specs), cards_pp)):
                if page_idx > 0:
                    printer.newPage()
                page_specs = specs[page_start: page_start + cards_pp]

                painter.setFont(QFont("Arial", 9, QFont.Bold))
                painter.setPen(QColor("#37474F"))
                painter.drawText(
                    QRectF(margin_l, margin_t * 0.3,
                           page_w_px - margin_l * 2, hdr_h_px * 0.55),
                    Qt.AlignLeft | Qt.AlignVCenter, run_label,
                )
                painter.setFont(QFont("Arial", 8))
                painter.setPen(QColor("#78909C"))
                painter.drawText(
                    QRectF(margin_l, margin_t * 0.3,
                           page_w_px - margin_l * 2, hdr_h_px * 0.55),
                    Qt.AlignRight | Qt.AlignVCenter,
                    f"Page {page_idx + 1} / {total_pg}  •  {len(specs)} unique vials",
                )
                rule_y = margin_t + hdr_h_px * 0.65
                painter.setPen(QPen(QColor("#B0BEC5"), max(1.0, mm2px * 0.25)))
                painter.drawLine(QPointF(margin_l, rule_y),
                                 QPointF(page_w_px - margin_l, rule_y))

                content_top = margin_t + hdr_h_px
                for idx, spec in enumerate(page_specs):
                    col = idx % n_cols
                    row = idx // n_cols
                    x = margin_l + col * (card_w_px + gap_h_px)
                    y = content_top + row * (card_h_px + gap_v_px)
                    painter.save()
                    painter.translate(x, y)
                    _label_renderer.render(
                        painter, card_w_px, card_h_px,
                        card_w_mm, card_h_mm, spec,
                        show_qr=show_qr,
                    )
                    painter.restore()
        finally:
            painter.end()
        return total_pg

    def _print_loadlist(self):
        try:
            specs, type_map = self._fetch_loadlist_specs()
            tray_cfg = self._fetch_tray_config()
        except Exception as exc:
            logging.error("LSC load list fetch: %s", exc, exc_info=True)
            QMessageBox.critical(self, "Error", str(exc))
            return

        if not specs:
            QMessageBox.information(self, "Empty", "No vial entries found.")
            return

        if tray_cfg:
            layout_note = (
                f"Tray grid: {tray_cfg[0]} tray(s) · "
                f"{tray_cfg[1]} rows × {tray_cfg[2]} cols"
            )
        else:
            layout_note = "card grid (no tray config found)"

        msg = QMessageBox(self)
        msg.setWindowTitle("Print Load List")
        msg.setText(
            f"<b>{len(specs)} unique vials</b><br>"
            f"<small>Layout: {layout_note}</small><br><br>"
            "Choose print format:"
        )
        msg.setIcon(QMessageBox.Question)
        btn_labels = msg.addButton("Labels  (with QR)",  QMessageBox.AcceptRole)
        btn_list   = msg.addButton("Load List  (no QR)", QMessageBox.AcceptRole)
        msg.addButton(QMessageBox.Cancel)
        msg.exec_()

        clicked = msg.clickedButton()
        if clicked is None or clicked == msg.button(QMessageBox.Cancel):
            return
        show_qr = (clicked is btn_labels)

        printer = QPrinter(QPrinter.HighResolution)
        try:
            from settings_module import apply_print_settings
            apply_print_settings(printer)
        except Exception:
            printer.setPageSize(QPrinter.A4)
            printer.setOrientation(QPrinter.Portrait)
        printer.setPageMargins(0, 0, 0, 0, QPrinter.Millimeter)

        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle("Print Labels" if show_qr else "Print Load List")
        if dlg.exec_() != QPrintDialog.Accepted:
            return

        if tray_cfg:
            total_pg = self._render_tray_loadlist(printer, specs, tray_cfg, show_qr=show_qr, type_map=type_map)
        else:
            total_pg = self._render_loadlist(printer, specs, show_qr=show_qr)

        if printer.outputFormat() == QPrinter.PdfFormat:
            mode = "Labels" if show_qr else "Load List"
            QMessageBox.information(
                self, "Saved",
                f"{mode} saved to:\n{printer.outputFileName()}\n\n"
                f"{len(specs)} unique vials · {total_pg} page(s).",
            )

    def _export(self):
        p, _ = QFileDialog.getSaveFileName(
            self, 'Export Run Data', f'lsc_run_{self.run_id}.xlsx', 'Excel (*.xlsx)'
        )
        if not p:
            return
        try:
            with db_manager.get_engine().connect() as sa_conn:
                df_ll = pd.read_sql(
                    text('SELECT * FROM vwlscloadlist WHERE runid=:rid'),
                    sa_conn, params={'rid': self.run_id}
                )
                df_res = pd.read_sql(text("""
                    SELECT
                        lr.AnalysisID       AS "Analysis ID",
                        s.Prefix            AS "Prefix",
                        s.SampleID          AS "Sample ID",
                        s.sName             AS "Name",
                        s.CollectionDate    AS "Collection Date",
                        lr.FinalActivity    AS "Activity",
                        lr.FinalActivityUnc AS "Activity Unc",
                        u.ShortName         AS "Unit",
                        lr.EnrichmentFactor     AS "EF",
                        lr.EnrichmentFactorUnc  AS "EF Unc",
                        lr.EnrichmentFactorMethod AS "EF Method",
                        lr.LLDStatus        AS "LLD Status",
                        lr.RejectFlag       AS "Rejected"
                    FROM TRIMS.LSCResult lr
                    JOIN TRIMS.LSCLoadList ll
                        ON ll.AnalysisID = lr.AnalysisID AND ll.RunID = lr.RunID
                    JOIN Analysis a  ON a.AnalysisID   = lr.AnalysisID
                    JOIN Sample   s  ON s.SampleID     = a.SampleID AND s.Prefix = a.Prefix
                    LEFT JOIN MeasurementUnit u ON u.UnitID = lr.ActivityUnit
                    WHERE lr.RunID = :rid
                      AND (ll.IsIgnored IS NULL OR ll.IsIgnored = FALSE)
                    ORDER BY ll.PositionInRun
                """), sa_conn, params={'rid': self.run_id})

            with pd.ExcelWriter(p, engine='openpyxl') as xw:
                df_ll.to_excel(xw, index=False, sheet_name='Load List')
                if not df_res.empty:
                    df_res.to_excel(xw, index=False, sheet_name='Results')

            QMessageBox.information(self, 'Exported', f'Run data exported to:\n{p}')
        except Exception as e:
            logging.error(f'LSC export failed: {e}', exc_info=True)
            QMessageBox.critical(self, 'Export Error', f'Failed to export:\n{e}')


    def _on_import_dialog_finished(self, result):
        if result == QDialog.Accepted:
            self.refresh_all()
            self._qc_widget.refresh()

    def _open_import(self):
        # Warn if run is finalized
        try:
            with db_manager.get_connection() as conn:
                run_row = conn.execute(text(
                    "SELECT IsLocked FROM TRIMS.LSCRun WHERE RunID = :rid"
                ), {'rid': self.run_id}).fetchone()
                is_locked = bool(getattr(run_row, 'IsLocked', False)) if run_row else False
        except Exception:
            is_locked = False

        if is_locked:
            reply = QMessageBox.warning(
                self, "Run is Finalized",
                f"Run {self.run_id} has been signed off and finalized.\n\n"
                "Re-importing will overwrite existing results and may change finalized values.\n\n"
                "Do you want to proceed?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel
            )
            if reply != QMessageBox.Yes:
                return

        dlg = TrimsLSCImportDialog(self.run_id, self)
        dlg.finished.connect(self._on_import_dialog_finished)
        dlg.show()

