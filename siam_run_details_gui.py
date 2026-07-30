"""
siam_run_details_gui.py — SIAM Analysis Run Details for IsoWorks.

Displays run metadata, load list, and pivoted results for a SIAM run.
Provides Import (subprocess), Edit metadata, Print Load List, Export to Excel.
Finalize stays in siam_finalize_dialog.py — not duplicated here.

Entry point: SiamRunDetailsWindow(run_id, parent=None)
"""
from __future__ import annotations

import logging
import os
import sys
import subprocess

import numpy as np
import pandas as pd

from PyQt5.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLineEdit, QPushButton, QTableView, QLabel,
    QFileDialog, QTabWidget, QMessageBox, QComboBox,
    QAbstractItemView, QCheckBox, QHeaderView, QDesktopWidget,
    QApplication, QDateTimeEdit,
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QFont, QPainter, QColor, QPen
from PyQt5.QtCore import Qt, QRectF, QPointF, QDateTime


class _MultiLineHeader(QHeaderView):
    """Horizontal header that renders '\\n' in section labels as two stacked lines."""

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setDefaultAlignment(Qt.AlignCenter)

    def sizeHint(self):
        s = super().sizeHint()
        s.setHeight(self.fontMetrics().height() * 2 + 14)
        return s

    def paintSection(self, painter, rect, logical_index):
        model = self.model()
        label = (model.headerData(logical_index, Qt.Horizontal, Qt.DisplayRole)
                 if model else None) or ""
        label = str(label)

        # Let Qt draw the full section (background, borders, hover, sort indicator)
        painter.save()
        super().paintSection(painter, rect, logical_index)
        painter.restore()

        # Single-line labels are fully handled by super() above
        if "\n" not in label:
            return

        # Multi-line: erase super()'s single-line text, then draw two lines
        painter.save()
        painter.fillRect(rect.adjusted(1, 1, -1, -1), self.palette().button())

        # Honour model alignment role (Right for numeric columns, Centre otherwise)
        raw_align = (model.headerData(logical_index, Qt.Horizontal, Qt.TextAlignmentRole)
                     if model else None)
        h_align = Qt.AlignRight if (raw_align and int(raw_align) & Qt.AlignRight) else Qt.AlignHCenter

        lines = label.split("\n")
        fm = painter.fontMetrics()
        lh = fm.height()
        gap = 2
        total_h = lh * len(lines) + gap * (len(lines) - 1)
        y = rect.top() + max(2, (rect.height() - total_h) // 2)
        painter.setPen(self.palette().buttonText().color())
        for line in lines:
            painter.drawText(rect.left() + 4, y, rect.width() - 8, lh,
                             h_align | Qt.AlignTop, line)
            y += lh + gap
        painter.restore()
from PyQt5.QtPrintSupport import QPrinter, QPrintDialog

from db_core import db_manager
from sqlalchemy import text
from shared_utils import format_person_name, get_current_user_id, check_employee_privilege
from sample_label_gui import LabelRenderer, analysis_spec
from tray_print_renderer import render_tray_loadlist as _render_tray, default_tray_cfg
from validation_gui import QCStatusWidget

_label_renderer = LabelRenderer()

# ── Load-list PDF layout constants (all in mm) ────────────────────────────────
# Labels (with QR): 3 columns
_LL_COLS      = 3
_LL_CARD_W    = 62.0
_LL_CARD_H    = 30.0
# Load list (no QR): 4 columns, slightly smaller cards
_LL_COLS_NOQR    = 4
_LL_CARD_W_NOQR  = 46.0
_LL_CARD_H_NOQR  = 26.0

_LL_GAP_H     = 3.0    # horizontal gap between card columns
_LL_GAP_V     = 2.5    # vertical gap between card rows
_LL_MARGIN_L  = 8.0    # left/right page margin
_LL_MARGIN_T  = 10.0   # top page margin
_LL_HDR_H     = 14.0   # run-info header height

# ---------------------------------------------------------------------------
_META_STYLE = """
QGroupBox {
    font-weight: bold; font-size: 12px;
    border: 1px solid #C8D0DC; border-radius: 6px;
    margin-top: 8px; padding: 6px 8px;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #2D4A8A; }
QLabel.header-label { color: #3A3A44; font-size: 12.5px; font-weight: 500; }
QLineEdit, QComboBox, QTextEdit { background: #FFFFFF; border: 1px solid #D0D0DD;
    border-radius: 6px; padding: 4px 6px; font-size: 12.5px; }
QLineEdit:read-only { background: #F5F6FA; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #4C82FF; }
QDateTimeEdit::drop-down { background: #C5CAE9; border: none; width: 22px; border-radius: 0 6px 6px 0; }
QDateTimeEdit::drop-down:hover { background: #7986CB; }
QDateTimeEdit::drop-down:pressed { background: #3949AB; }
QDateTimeEdit::down-arrow { image: none; width: 0; height: 0; border-left: 5px solid transparent; border-right: 5px solid transparent; border-top: 6px solid #3949AB; }
"""

_TABLE_STYLE = """
QTableView {
    border: 1px solid #E0E4EC; border-radius: 4px;
    background: #FFFFFF; alternate-background-color: #F5F7FC;
    gridline-color: #E8EAF0; font-size: 12px;
}
QTableView::item { padding: 4px 8px; }
QTableView::item:selected { background: #DDEEFF; color: #000; }
QHeaderView::section {
    background: #EEF1F7; color: #444; font-weight: bold;
    border: none; border-bottom: 2px solid #C8D0DC;
    padding: 5px 8px; font-size: 12px;
}
"""

_HEADER_COLOR = "#2D4A8A"


class SiamRunDetailsWindow(QDialog):
    """
    SIAM run details viewer with Edit, Import, Print and Export commands.
    """

    def __init__(self, run_id, parent=None):
        super().__init__(parent)
        self.run_id = run_id
        self._equipment_id = None   # populated in refresh_all
        self.current_user = get_current_user_id()
        self.setWindowTitle(f"SIAM Run {run_id}")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setStyleSheet(_META_STYLE)

        screen = QDesktopWidget().screenGeometry(0)
        w, h = 1280, 880
        self.setGeometry(
            screen.left() + (screen.width() - w) // 2,
            screen.top() + (screen.height() - h) // 2,
            w, h
        )

        try:
            db_manager.get_engine()
        except Exception:
            QMessageBox.critical(self, "Error", "Database connection is not set.")
            return

        self._layout = QVBoxLayout(self)
        self._init_toolbar()
        self._init_metadata()
        self._init_tabs()
        self._populate_combos()
        self.refresh_all()

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------

    def _init_toolbar(self):
        bar = QHBoxLayout()
        bar.addStretch()

        self.btnEdit = QPushButton("Edit")
        self.btnEdit.setCheckable(True)
        self.btnEdit.toggled.connect(self._toggle_edit)

        self.btnImport = QPushButton("Import")
        self.btnImport.clicked.connect(self._open_import)

        self.btnPrint = QPushButton("Print Load List")
        self.btnPrint.clicked.connect(self._print_loadlist)

        self.btnExport = QPushButton("Export Run Data")
        self.btnExport.clicked.connect(self._export)

        self.btnQCReview = QPushButton("QC / Validate")
        self.btnQCReview.setStyleSheet(
            "QPushButton { background:#2D6A2D; color:white; font-weight:bold;"
            " padding:4px 12px; border-radius:3px; }"
            "QPushButton:hover { background:#3A8A3A; }"
        )
        self.btnQCReview.clicked.connect(self._open_qc_tab)

        btnClose = QPushButton("Close")
        btnClose.clicked.connect(self.accept)

        for btn in [self.btnEdit, self.btnImport, self.btnPrint, self.btnExport,
                    self.btnQCReview, btnClose]:
            bar.addWidget(btn)

        self._layout.addLayout(bar)

    # ------------------------------------------------------------------
    # Metadata section (two groups side-by-side)
    # ------------------------------------------------------------------

    def _init_metadata(self):
        def lbl(text):
            l = QLabel(text)
            l.setProperty("class", "header-label")
            l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return l

        # --- Run Metadata group ---
        self.grpMeta = QGroupBox("Run Metadata & Staff")
        grid = QGridLayout()
        grid.setContentsMargins(10, 5, 10, 5)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(4)

        self.txtRunID = QLineEdit(str(self.run_id))
        self.txtRunID.setReadOnly(True)

        self.cmbStatus = QComboBox()
        self.cmbStatus.setEnabled(False)

        self.dtStart = QDateTimeEdit(); self.dtStart.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dtStart.setCalendarPopup(True); self.dtStart.setReadOnly(True)
        self.dtStart.setMinimumDateTime(QDateTime(1900, 1, 1, 0, 0))
        self.dtStart.setSpecialValueText("—")
        self.dtEnd = QDateTimeEdit(); self.dtEnd.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dtEnd.setCalendarPopup(True); self.dtEnd.setReadOnly(True)
        self.dtEnd.setMinimumDateTime(QDateTime(1900, 1, 1, 0, 0))
        self.dtEnd.setSpecialValueText("—")

        self.cmbProcedure = QComboBox()
        self.cmbProcedure.setEnabled(False)

        self.txtEquipment = QLineEdit()
        self.txtEquipment.setReadOnly(True)

        self.cmbTech1 = QComboBox()
        self.cmbTech1.setEnabled(False)
        self.cmbTech2 = QComboBox()
        self.cmbTech2.setEnabled(False)

        self.txtPath = QLineEdit()
        self.txtPath.setReadOnly(True)

        self.txtRemarks = QLineEdit()
        self.txtRemarks.setReadOnly(True)

        grid.addWidget(lbl("Run No."),       0, 0); grid.addWidget(self.txtRunID,    0, 1)
        grid.addWidget(lbl("Status"),        0, 2); grid.addWidget(self.cmbStatus,   0, 3)
        grid.addWidget(lbl("Start time"),    1, 0); grid.addWidget(self.dtStart,    1, 1)
        grid.addWidget(lbl("End time"),      1, 2); grid.addWidget(self.dtEnd,      1, 3)
        grid.addWidget(lbl("Procedure"),     2, 0); grid.addWidget(self.cmbProcedure,2, 1)
        grid.addWidget(lbl("Equipment"),     2, 2); grid.addWidget(self.txtEquipment,2, 3)
        grid.addWidget(lbl("Technician 1"),  3, 0); grid.addWidget(self.cmbTech1,   3, 1)
        grid.addWidget(lbl("Technician 2"),  3, 2); grid.addWidget(self.cmbTech2,   3, 3)
        grid.addWidget(lbl("Data Path"),     4, 0); grid.addWidget(self.txtPath,    4, 1, 1, 3)
        grid.addWidget(lbl("Remarks"),       5, 0); grid.addWidget(self.txtRemarks, 5, 1, 1, 3)
        self.grpMeta.setLayout(grid)

        # --- Run info / measurables group ---
        self.grpInfo = QGroupBox("Run Info")
        igrid = QGridLayout()
        igrid.setContentsMargins(10, 5, 10, 5)
        igrid.setHorizontalSpacing(20)
        igrid.setVerticalSpacing(4)

        self.txtMeasurables = QLineEdit()
        self.txtMeasurables.setReadOnly(True)
        self.txtOutlierMethod = QLineEdit()
        self.txtOutlierMethod.setReadOnly(True)
        self.txtSampleCount = QLineEdit()
        self.txtSampleCount.setReadOnly(True)
        self.txtRepeatCount = QLineEdit()
        self.txtRepeatCount.setReadOnly(True)

        igrid.addWidget(lbl("Measurables"),     0, 0); igrid.addWidget(self.txtMeasurables,   0, 1, 1, 3)
        igrid.addWidget(lbl("Outlier Method"),  1, 0); igrid.addWidget(self.txtOutlierMethod,  1, 1)
        igrid.addWidget(lbl("Samples"),         2, 0); igrid.addWidget(self.txtSampleCount,   2, 1)
        igrid.addWidget(lbl("Total Repeats"),   2, 2); igrid.addWidget(self.txtRepeatCount,   2, 3)
        self.grpInfo.setLayout(igrid)

        container = QWidget()
        top = QGridLayout(container)
        top.setContentsMargins(6, 4, 6, 4)
        top.setHorizontalSpacing(14)
        top.addWidget(self.grpMeta, 0, 0)
        top.addWidget(self.grpInfo, 0, 1)
        top.setColumnStretch(0, 3)
        top.setColumnStretch(1, 2)
        self._layout.addWidget(container)

    # ------------------------------------------------------------------
    # Tabs: Load List + Results
    # ------------------------------------------------------------------

    def _init_tabs(self):
        # Unknowns-only toggle sits above the tabs, filters both views
        self.chkUnknownsOnly = QCheckBox("Unknown Samples Only")
        self.chkUnknownsOnly.setChecked(True)
        self.chkUnknownsOnly.toggled.connect(self._on_unknowns_toggled)
        chk_row = QHBoxLayout()
        chk_row.addWidget(self.chkUnknownsOnly)
        chk_row.addStretch()
        self._layout.addLayout(chk_row)

        self.tabs = QTabWidget()
        self._layout.addWidget(self.tabs, 1)

        # Load List tab
        tabLL = QWidget()
        ll_layout = QVBoxLayout(tabLL)
        self.tblLL = QTableView()
        self.modLL = QStandardItemModel()
        self.tblLL.setModel(self.modLL)
        ll_layout.addWidget(self.tblLL)
        self.tabs.addTab(tabLL, "Load List")

        # Results tab
        tabRes = QWidget()
        res_layout = QVBoxLayout(tabRes)
        self.tblRes = QTableView()
        self.modRes = QStandardItemModel()
        self.tblRes.setModel(self.modRes)
        _res_hdr = _MultiLineHeader(self.tblRes)
        self.tblRes.setHorizontalHeader(_res_hdr)
        res_layout.addWidget(self.tblRes)
        self.tabs.addTab(tabRes, "Analysis Results")

        for tbl in [self.tblLL, self.tblRes]:
            self._style_table(tbl)

        # QC / Validation tab
        tabQC = QWidget()
        qc_lay = QVBoxLayout(tabQC)
        qc_lay.setContentsMargins(8, 8, 8, 8)
        self._qc_widget = QCStatusWidget(
            module         = "SIAM",
            run_id         = self.run_id,
            parent_refresh = self.refresh_all,
            parent         = tabQC,
        )
        self._qc_widget.validated.connect(self.refresh_all)
        qc_lay.addWidget(self._qc_widget)
        self.tabs.addTab(tabQC, "QC / Validation")

    def _style_table(self, table):
        table.setStyleSheet(_TABLE_STYLE)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        table.verticalHeader().setDefaultSectionSize(26)
        table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        table.setSortingEnabled(True)

    # ------------------------------------------------------------------
    # Populate combos
    # ------------------------------------------------------------------

    def _populate_combos(self):
        try:
            with db_manager.get_connection() as conn:
                self.cmbStatus.clear()
                for r in conn.execute(text(
                    "SELECT Status, Description FROM StatusLookup "
                    "WHERE Status IN (7, 8, 98, -9) ORDER BY Status"
                )).fetchall():
                    self.cmbStatus.addItem(r.Description, r.Status)

                self.cmbProcedure.clear()
                self.cmbProcedure.addItem("", None)
                for r in conn.execute(text(
                    "SELECT ProcedureID, ProcedureName FROM AnalysisProcedure ORDER BY ProcedureName"
                )).fetchall():
                    self.cmbProcedure.addItem(r.ProcedureName, r.ProcedureID)

                self.cmbTech1.clear()
                self.cmbTech2.clear()
                self.cmbTech1.addItem("", None)
                self.cmbTech2.addItem("", None)
                for r in conn.execute(text(
                    "SELECT EmployeeID, LastName, FirstMiddleName FROM Employee ORDER BY LastName"
                )).fetchall():
                    name = format_person_name(r.LastName, r.FirstMiddleName)
                    self.cmbTech1.addItem(name, r.EmployeeID)
                    self.cmbTech2.addItem(name, r.EmployeeID)
        except Exception as e:
            logging.error(f"SIAM details: populate combos failed: {e}")

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh_all(self):
        self._refresh_metadata()
        self._refresh_loadlist()
        self._refresh_results()

    def _on_unknowns_toggled(self):
        self._refresh_loadlist()
        self._refresh_results()

    def _refresh_metadata(self):
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT r.RunStartTime, r.RunEndTime, r.DataPath, r.Remarks,
                           r.RunStatus, r.ProcedureID, r.TechnicianID, r.Technician2,
                           r.Measurables, r.OutlierMethod, r.IsLocked,
                           r.EquipmentID, e.EquipmentName
                    FROM SIAM.SIAnalysisRun r
                    LEFT JOIN Equipment e ON r.EquipmentID = e.EquipmentID
                    WHERE r.SIAnalysisRunID = :rid
                """), {'rid': self.run_id}).fetchone()

                if not row:
                    QMessageBox.critical(self, "Error", f"Run {self.run_id} not found.")
                    self.close()
                    return

                def _fmt_dt(v):
                    if not v:
                        return ""
                    s = str(v)
                    # Trim to "YYYY-MM-DD HH:MM"
                    return s[:16] if len(s) >= 16 else s

                _st = row.RunStartTime
                if _st and hasattr(_st, 'year'):
                    self.dtStart.setDateTime(QDateTime(_st.year, _st.month, _st.day, _st.hour, _st.minute))
                else:
                    self.dtStart.setDateTime(self.dtStart.minimumDateTime())
                _et = row.RunEndTime
                if _et and hasattr(_et, 'year'):
                    self.dtEnd.setDateTime(QDateTime(_et.year, _et.month, _et.day, _et.hour, _et.minute))
                else:
                    self.dtEnd.setDateTime(self.dtEnd.minimumDateTime())
                self.txtPath.setText(row.DataPath or "")
                self.txtRemarks.setText(row.Remarks or "")
                self.txtEquipment.setText(row.EquipmentName or "")
                self._equipment_id = getattr(row, 'EquipmentID', None)
                self.txtMeasurables.setText(row.Measurables or "")
                self.txtOutlierMethod.setText(row.OutlierMethod or "")

                idx = self.cmbStatus.findData(row.RunStatus)
                if idx >= 0:
                    self.cmbStatus.setCurrentIndex(idx)

                idx = self.cmbProcedure.findData(row.ProcedureID)
                if idx >= 0:
                    self.cmbProcedure.setCurrentIndex(idx)

                idx = self.cmbTech1.findData(row.TechnicianID)
                if idx >= 0:
                    self.cmbTech1.setCurrentIndex(idx)

                idx = self.cmbTech2.findData(row.Technician2)
                if idx >= 0:
                    self.cmbTech2.setCurrentIndex(idx)

                # Sample / repeat counts
                counts = conn.execute(text("""
                    SELECT COUNT(DISTINCT ll.AnalysisID) AS samples,
                           COUNT(*)                      AS repeats
                    FROM SIAM.SIAnalysisLoadList ll
                    WHERE ll.SIAnalysisRunID = :rid
                """), {'rid': self.run_id}).fetchone()
                self.txtSampleCount.setText(str(counts.samples if counts else 0))
                self.txtRepeatCount.setText(str(counts.repeats if counts else 0))

                self._is_locked = bool(getattr(row, 'IsLocked', False))
                if self._is_locked:
                    self.setWindowTitle(f"SIAM Run {self.run_id}  [FINALIZED]")

                # Check if results already exist → rename Import button
                has_results = conn.execute(text("""
                    SELECT EXISTS (
                        SELECT 1 FROM SIAM.SIAnalysisResult res
                        JOIN SIAM.SIAnalysisLoadList ll ON ll.SIAnalysisID = res.SIAnalysisID
                        WHERE ll.SIAnalysisRunID = :rid
                    )
                """), {'rid': self.run_id}).scalar()
                self.btnImport.setText("Re-Import" if has_results else "Import")

                # Edit is always available — locked runs allow metadata-only edits
                # (technician, path, remarks); Status/Procedure are restricted in _toggle_edit

        except Exception as e:
            logging.error(f"SIAM details: refresh metadata failed: {e}", exc_info=True)

    def _refresh_loadlist(self):
        self.modLL.clear()
        try:
            with db_manager.get_connection() as conn:
                sql = f"""
                    SELECT ll.PositionInRun,
                           ll.TrayNumber, ll.PositionInTray,
                           ll.InstrumentAnalysisID,
                           s.Prefix || '-' || CAST(s.SampleID AS VARCHAR) AS SampleCode,
                           s.sName,
                           a.AnalysisID, ll.Repeat, a.Repeats,
                           {db_manager.sql_concat('CAST(ll.Repeat AS VARCHAR)', "' / '", 'CAST(a.Repeats AS VARCHAR)')} AS RepeatFrac,
                           ll.Remarks
                    FROM SIAM.SIAnalysisLoadList ll
                    JOIN Analysis a  ON ll.AnalysisID = a.AnalysisID
                    JOIN Sample   s  ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix
                    WHERE ll.SIAnalysisRunID = :rid
                """
                if self.chkUnknownsOnly.isChecked():
                    sql += " AND s.SampleType = 0"
                sql += " ORDER BY ll.PositionInRun"

                rows = conn.execute(text(sql), {'rid': self.run_id}).fetchall()

            self.modLL.setHorizontalHeaderLabels([
                "Vial #", "Run Order", "Inst ID", "Sample ID",
                "Analysis ID", "Name", "Repeat", "Remarks",
            ])
            for r in rows:
                if r.TrayNumber is not None and r.PositionInTray is not None:
                    vial = f"{r.TrayNumber}-{r.PositionInTray}"
                elif r.TrayNumber is not None:
                    vial = str(r.TrayNumber)
                else:
                    vial = ""
                self.modLL.appendRow([
                    QStandardItem(vial),
                    QStandardItem(str(r.PositionInRun)),
                    QStandardItem(str(r.InstrumentAnalysisID or "")),
                    QStandardItem(r.SampleCode or ""),
                    QStandardItem(str(r.AnalysisID)),
                    QStandardItem(r.sName or ""),
                    QStandardItem(r.RepeatFrac or ""),
                    QStandardItem(r.Remarks or ""),
                ])
            hdr = self.tblLL.horizontalHeader()
            hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
            hdr.setSectionResizeMode(5, QHeaderView.Interactive)   # Name column
            self.tblLL.setColumnWidth(5, 180)
        except Exception as e:
            logging.error(f"SIAM details: refresh load list failed: {e}", exc_info=True)

    def _refresh_results(self):
        self.modRes.clear()
        try:
            with db_manager.get_connection() as conn:
                unknowns_filter = "AND s.SampleType = 0" if self.chkUnknownsOnly.isChecked() else ""
                rows = conn.execute(text(f"""
                    SELECT a.AnalysisID,
                           s.Prefix || '-' || CAST(s.SampleID AS VARCHAR) AS SampleCode,
                           s.sName,
                           res.MeasurableID                             AS MID,
                           m.AnalyteName                                AS MName,
                           COALESCE(mu.ShortName, '')                   AS Unit,
                           res.fValue, res.fValueUnc
                    FROM SIAM.SIAnalysisResult res
                    JOIN Analytes m                 ON m.AnalyteID      = res.MeasurableID
                    LEFT JOIN MeasurementUnit mu    ON mu.UnitID        = m.UnitID
                    JOIN SIAM.SIAnalysisLoadList ll ON ll.SIAnalysisID  = res.SIAnalysisID
                    JOIN Analysis a                 ON a.AnalysisID     = ll.AnalysisID
                    JOIN Sample   s                 ON s.SampleID = a.SampleID
                                                   AND s.Prefix   = a.Prefix
                    WHERE ll.SIAnalysisRunID = :rid
                      {unknowns_filter}
                    ORDER BY a.AnalysisID, res.MeasurableID
                """), {'rid': self.run_id}).fetchall()

            if not rows:
                self.modRes.setHorizontalHeaderLabels(["Info"])
                self.modRes.appendRow([QStandardItem("No results for this run.")])
                return

            df = pd.DataFrame(
                [tuple(r) for r in rows],
                columns=['analysisid', 'samplecode', 'sname',
                         'mid', 'mname', 'unit', 'fvalue', 'fvalueunc']
            )

            def _fmt(v, u, decimals=2):
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    return ""
                val = f"{float(v):.{decimals}f}"
                if u is not None and not (isinstance(u, float) and pd.isna(u)):
                    return f"{val} ± {float(u):.{decimals}f}"
                return val

            # Build mid→header map — only mids with actual results, no phantoms
            mid_meta = {}  # mid -> (mname, unit)
            for _, r in df.iterrows():
                if r['mid'] not in mid_meta:
                    mid_meta[r['mid']] = (r['mname'] or str(r['mid']), r['unit'])

            mid_header = {
                mid: f"{name}\n({unit})" if unit else name
                for mid, (name, unit) in mid_meta.items()
            }

            df['valstr'] = df.apply(lambda r: _fmt(r['fvalue'], r['fvalueunc']), axis=1)

            idx_cols = ['analysisid', 'samplecode', 'sname']
            pivot = df.pivot_table(index=idx_cols, columns='mid',
                                   values='valstr', aggfunc='first').reset_index()
            pivot.columns.name = None

            sorted_mids = sorted(mid_meta.keys())
            pivot.rename(columns=mid_header, inplace=True)

            # Column order: Sample ID | Name | Analysis ID || parameters…
            # Sample ID + Name are the natural identifiers (left side);
            # Analysis ID is a narrow numeric reference before the data columns.
            fixed_labels = ["Sample ID", "Name", "Analysis ID"]
            param_labels = [mid_header[m] for m in sorted_mids]
            self.modRes.clear()
            all_labels = fixed_labels + param_labels
            self.modRes.setHorizontalHeaderLabels(all_labels)

            # Align headers to match their data columns
            for i, lbl in enumerate(all_labels):
                align = Qt.AlignRight | Qt.AlignVCenter   # Analysis ID + param columns
                if i == 0:                                # Sample ID — left
                    align = Qt.AlignLeft | Qt.AlignVCenter
                elif i == 1:                              # Name — left
                    align = Qt.AlignLeft | Qt.AlignVCenter
                self.modRes.setHeaderData(i, Qt.Horizontal, int(align), Qt.TextAlignmentRole)

            for _, r in pivot.iterrows():
                items = []
                for col, align in [('samplecode',  Qt.AlignLeft  | Qt.AlignVCenter),
                                   ('sname',       Qt.AlignLeft  | Qt.AlignVCenter),
                                   ('analysisid',  Qt.AlignRight | Qt.AlignVCenter)]:
                    it = QStandardItem(str(r[col]) if r[col] is not None else "")
                    it.setTextAlignment(align)
                    items.append(it)
                for mid in sorted_mids:
                    cell = r.get(mid_header[mid], "")
                    it = QStandardItem("" if (cell is None or (isinstance(cell, float) and pd.isna(cell))) else str(cell))
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    items.append(it)
                self.modRes.appendRow(items)

            # Re-install custom header in case modelReset disconnected it
            if not isinstance(self.tblRes.horizontalHeader(), _MultiLineHeader):
                hdr = _MultiLineHeader(self.tblRes)
                self.tblRes.setHorizontalHeader(hdr)
            hdr = self.tblRes.horizontalHeader()
            hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
            hdr.setSectionResizeMode(1, QHeaderView.Interactive)  # Name: fixed default, user can resize
            self.tblRes.setColumnWidth(1, 160)

        except Exception as e:
            logging.error(f"SIAM details: refresh results failed: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Edit toggle
    # ------------------------------------------------------------------

    def _toggle_edit(self, checked):
        self.btnEdit.setText("Stop Edit" if checked else "Edit")
        locked = getattr(self, '_is_locked', False)
        # Locked runs: only staff / path / remarks are editable; status & procedure are frozen
        self.cmbStatus.setEnabled(checked and not locked)
        self.cmbProcedure.setEnabled(checked and not locked)
        self.cmbTech1.setEnabled(checked)
        self.cmbTech2.setEnabled(checked)
        self.txtPath.setReadOnly(not checked)
        self.txtRemarks.setReadOnly(not checked)

        if not checked:
            self._save_metadata()
            self.refresh_all()

    def _save_metadata(self):
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE SIAM.SIAnalysisRun
                    SET DataPath    = :path,
                        Remarks     = :remarks,
                        ProcedureID = :pid,
                        TechnicianID = :t1,
                        Technician2  = :t2,
                        RunStatus    = :st
                    WHERE SIAnalysisRunID = :rid
                """), {
                    'path':    self.txtPath.text().strip(),
                    'remarks': self.txtRemarks.text().strip(),
                    'pid':     self.cmbProcedure.currentData(),
                    't1':      self.cmbTech1.currentData(),
                    't2':      self.cmbTech2.currentData(),
                    'st':      self.cmbStatus.currentData(),
                    'rid':     self.run_id,
                })
                conn.commit()
            logging.info(f"SIAM run {self.run_id} metadata saved.")
        except Exception as e:
            logging.error(f"SIAM details: save metadata failed: {e}", exc_info=True)
            QMessageBox.warning(self, "Save Failed", f"Could not save metadata:\n{e}")

    # ------------------------------------------------------------------
    # Import (launches siam_processor_gui.py as subprocess)
    # ------------------------------------------------------------------

    def _open_import(self):
        try:
            from PyQt5.QtCore import QProcess
            python_exe = sys.executable
            dialect = db_manager.dialect
            dsn = getattr(db_manager, '_path_or_dsn', '') or ''

            self._import_proc = QProcess(self)
            self._import_proc.finished.connect(self._on_import_closed)
            self.btnImport.setEnabled(False)
            self._import_proc.start(python_exe,
                                    ["-m", "siam_processor.views.main_window", str(self.run_id), dsn, dialect])
        except Exception as e:
            logging.error(f"SIAM import launch failed: {e}", exc_info=True)
            QMessageBox.critical(self, "Error", f"Could not launch import:\n{e}")
            self.btnImport.setEnabled(True)

    def _on_import_closed(self):
        self.btnImport.setEnabled(True)
        self._import_proc = None
        self.refresh_all()
        self._qc_widget.refresh()

    def _open_qc_tab(self):
        """Switch to the QC / Validation tab and refresh the widget."""
        self._qc_widget.refresh()
        self.tabs.setCurrentIndex(2)

    # ------------------------------------------------------------------
    # Print load list — A4 label-card grid
    # ------------------------------------------------------------------

    def _fetch_tray_config(self):
        """Return (traycount, trayrows, traycols) from equipmenttrayconfig, or None."""
        if not self._equipment_id:
            return None
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT traycount, trayrows, traycols
                    FROM public.equipmenttrayconfig
                    WHERE equipmentid = :eid
                """), {'eid': self._equipment_id}).fetchone()
            return (row.traycount, row.trayrows, row.traycols) if row else None
        except Exception as exc:
            logging.warning("Could not fetch SIAM tray config: %s", exc)
            return None

    def _fetch_loadlist_specs(self):
        """One LabelSpec per unique Vial # (TrayNumber-PositionInTray).
        Vials analysed at multiple Run Orders appear only once."""
        with db_manager.get_connection() as conn:
            rows = conn.execute(text("""
                SELECT DISTINCT ON (ll.TrayNumber, ll.PositionInTray)
                       ll.TrayNumber, ll.PositionInTray,
                       s.Prefix, s.SampleID,
                       a.AnalysisID
                FROM SIAM.SIAnalysisLoadList ll
                JOIN Analysis a ON ll.AnalysisID = a.AnalysisID
                JOIN Sample   s ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix
                WHERE ll.SIAnalysisRunID = :rid
                ORDER BY ll.TrayNumber, ll.PositionInTray, ll.AnalysisID
            """), {'rid': self.run_id}).fetchall()

        specs = []
        for r in rows:
            if r.TrayNumber is not None and r.PositionInTray is not None:
                vial = f"{r.TrayNumber}-{r.PositionInTray}"
            elif r.TrayNumber is not None:
                vial = str(r.TrayNumber)
            else:
                continue   # no vial info — skip
            specs.append(analysis_spec(
                prefix=r.Prefix or "",
                sample_id=str(r.SampleID),
                analysis_id=str(r.AnalysisID),
                container_num=vial,
                job_type="SIAM",
            ))
        return specs

    def _render_loadlist(self, printer: QPrinter, specs, show_qr: bool = True) -> int:
        """Paint label-card grid onto printer; returns total page count."""
        dpi   = printer.resolution()
        mm2px = dpi / 25.4

        # Pick layout constants based on mode
        n_cols    = _LL_COLS        if show_qr else _LL_COLS_NOQR
        card_w_mm = _LL_CARD_W      if show_qr else _LL_CARD_W_NOQR
        card_h_mm = _LL_CARD_H      if show_qr else _LL_CARD_H_NOQR

        page_w_px = 210.0    * mm2px
        page_h_px = 297.0    * mm2px
        card_w_px = card_w_mm * mm2px
        card_h_px = card_h_mm * mm2px
        gap_h_px  = _LL_GAP_H   * mm2px
        gap_v_px  = _LL_GAP_V   * mm2px
        margin_l  = _LL_MARGIN_L * mm2px
        margin_t  = _LL_MARGIN_T * mm2px
        hdr_h_px  = _LL_HDR_H   * mm2px

        avail_h  = page_h_px - margin_t - hdr_h_px
        rows_pp  = max(1, int((avail_h + gap_v_px) / (card_h_px + gap_v_px)))
        cards_pp = n_cols * rows_pp
        total_pg = (len(specs) + cards_pp - 1) // cards_pp

        mode_tag  = "Labels" if show_qr else "Load List"
        _st = self.dtStart.dateTime()
        _start_str = _st.toString("yyyy-MM-dd HH:mm") if _st != self.dtStart.minimumDateTime() else ""
        run_label = (
            f"SIAM Run {self.run_id}  ·  {mode_tag}   "
            f"Start: {_start_str or '—'}   "
            f"Equipment: {self.txtEquipment.text() or '—'}"
        )

        painter = QPainter(printer)
        try:
            for page_idx, page_start in enumerate(range(0, len(specs), cards_pp)):
                if page_idx > 0:
                    printer.newPage()

                page_specs = specs[page_start: page_start + cards_pp]

                # ── page header (fixed pt — device-independent) ──────────
                painter.setFont(QFont("Arial", 9, QFont.Bold))
                painter.setPen(QColor("#37474F"))
                painter.drawText(
                    QRectF(margin_l, margin_t * 0.3,
                           page_w_px - margin_l * 2, hdr_h_px * 0.55),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    run_label,
                )
                painter.setFont(QFont("Arial", 8))
                painter.setPen(QColor("#78909C"))
                painter.drawText(
                    QRectF(margin_l, margin_t * 0.3,
                           page_w_px - margin_l * 2, hdr_h_px * 0.55),
                    Qt.AlignRight | Qt.AlignVCenter,
                    f"Page {page_idx + 1} / {total_pg}  •  {len(specs)} unique vials",
                )

                # rule
                rule_y = margin_t + hdr_h_px * 0.65
                painter.setPen(QPen(QColor("#B0BEC5"), max(1.0, mm2px * 0.25)))
                painter.drawLine(QPointF(margin_l, rule_y),
                                 QPointF(page_w_px - margin_l, rule_y))

                # ── cards ────────────────────────────────────────────────
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
            specs = self._fetch_loadlist_specs()
        except Exception as exc:
            logging.error("SIAM load list fetch: %s", exc, exc_info=True)
            QMessageBox.critical(self, "Error", str(exc))
            return

        if not specs:
            QMessageBox.information(self, "Empty", "No vial entries found.")
            return

        # ── resolve tray config early (needed for dialog labels and printing) ──
        tray_cfg = self._fetch_tray_config()
        if tray_cfg is None:
            tray_cfg = default_tray_cfg(specs, default_cols=8)
        _tc, _tr, _tcol = tray_cfg
        _tray_s = "tray" if _tc == 1 else "trays"

        # ── choose format ────────────────────────────────────────────────
        lbl_labels = (
            f"Labels  ({_LL_COLS} col · {int(_LL_CARD_W)}×{int(_LL_CARD_H)} mm · with QR)"
        )
        lbl_list = (
            f"Load List  ({_tc} {_tray_s} · {_tr} rows × {_tcol} cols · no QR)"
        )
        msg = QMessageBox(self)
        msg.setWindowTitle("Print Load List")
        msg.setText(
            f"<b>{len(specs)} unique vials</b><br><br>"
            "Choose print format:"
        )
        msg.setIcon(QMessageBox.Question)
        btn_labels = msg.addButton(lbl_labels, QMessageBox.AcceptRole)
        btn_list   = msg.addButton(lbl_list,   QMessageBox.AcceptRole)
        msg.addButton(QMessageBox.Cancel)
        msg.exec_()

        clicked = msg.clickedButton()
        if clicked is None or clicked == msg.button(QMessageBox.Cancel):
            return
        show_qr = clicked is btn_labels

        # ── print / save ─────────────────────────────────────────────────
        printer = QPrinter(QPrinter.HighResolution)
        try:
            from settings_module import apply_print_settings
            apply_print_settings(printer)
        except Exception:
            printer.setPageSize(QPrinter.A4)
            printer.setPageMargins(0, 0, 0, 0, QPrinter.Millimeter)
            printer.setOrientation(QPrinter.Portrait)

        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle(
            "Print Labels" if show_qr else "Print Load List"
        )
        if dlg.exec_() != QPrintDialog.Accepted:
            return

        if show_qr:
            total_pg = self._render_loadlist(printer, specs, show_qr=True)
        else:
            equip_name = self.txtEquipment.text() if hasattr(self, 'txtEquipment') else ''
            start_str  = self.txtStart.text()     if hasattr(self, 'txtStart')     else ''
            total_pg = _render_tray(
                printer, specs, tray_cfg, show_qr=False,
                run_id=str(self.run_id), module_name="SIAM",
                equip_name=equip_name, start_str=start_str,
            )
        if printer.outputFormat() == QPrinter.PdfFormat:
            mode = "Labels" if show_qr else "Load List"
            QMessageBox.information(
                self, "Saved",
                f"{mode} saved to:\n{printer.outputFileName()}\n\n"
                f"{len(specs)} unique vials · {total_pg} page(s).",
            )

    # ------------------------------------------------------------------
    # Export to Excel
    # ------------------------------------------------------------------

    def _export(self):
        try:
            import openpyxl
        except ImportError:
            QMessageBox.critical(self, "Missing Library",
                                 "openpyxl is required for Excel export.\n"
                                 "Install with: pip install openpyxl")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Run Data", f"siam_run_{self.run_id}.xlsx", "Excel (*.xlsx)"
        )
        if not path:
            return

        try:
            with db_manager.get_engine().connect() as sa_conn:
                df_ll = pd.read_sql(text("""
                    SELECT ll.PositionInRun      AS "Run Order",
                           CASE WHEN ll.TrayNumber IS NOT NULL AND ll.PositionInTray IS NOT NULL
                                THEN CAST(ll.TrayNumber AS VARCHAR) || '-' || CAST(ll.PositionInTray AS VARCHAR)
                                ELSE '' END      AS "Vial #",
                           ll.InstrumentAnalysisID AS "Inst ID",
                           s.Prefix || '-' || CAST(s.SampleID AS VARCHAR) AS "Sample ID",
                           s.sName               AS "Name",
                           a.AnalysisID          AS "Analysis ID",
                           ll.Repeat, a.Repeats, ll.Remarks
                    FROM SIAM.SIAnalysisLoadList ll
                    JOIN Analysis a ON ll.AnalysisID = a.AnalysisID
                    JOIN Sample   s ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix
                    WHERE ll.SIAnalysisRunID = :rid
                    ORDER BY ll.PositionInRun
                """), sa_conn, params={'rid': self.run_id})

                df_res = pd.read_sql(text("""
                    SELECT a.AnalysisID,
                           s.Prefix || '-' || CAST(s.SampleID AS VARCHAR) AS samplecode,
                           s.sName,
                           res.MeasurableID                             AS mid,
                           m.AnalyteName                                AS mname,
                           COALESCE(mu.ShortName, '')                   AS unit,
                           res.fValue, res.fValueUnc
                    FROM SIAM.SIAnalysisResult res
                    JOIN Analytes m                 ON m.AnalyteID      = res.MeasurableID
                    LEFT JOIN MeasurementUnit mu    ON mu.UnitID        = m.UnitID
                    JOIN SIAM.SIAnalysisLoadList ll ON ll.SIAnalysisID  = res.SIAnalysisID
                    JOIN Analysis a                 ON a.AnalysisID     = ll.AnalysisID
                    JOIN Sample   s                 ON s.SampleID = a.SampleID
                                                   AND s.Prefix   = a.Prefix
                    WHERE ll.SIAnalysisRunID = :rid
                    ORDER BY a.AnalysisID, res.MeasurableID
                """), sa_conn, params={'rid': self.run_id})

            with pd.ExcelWriter(path, engine='openpyxl') as xw:
                df_ll.to_excel(xw, sheet_name="Load List", index=False)
                if not df_res.empty:
                    # Long format
                    df_long = df_res.rename(columns={
                        'analysisid': 'Analysis ID', 'samplecode': 'Sample ID',
                        'sname': 'Name', 'mid': 'MeasurableID',
                        'mname': 'Parameter', 'unit': 'Unit',
                        'fvalue': 'Value', 'fvalueunc': 'Uncertainty'
                    })
                    df_long.to_excel(xw, sheet_name="Results (Long)", index=False)

                    # mid→header map
                    mid_meta = {}
                    for _, r in df_res.iterrows():
                        if r['mid'] not in mid_meta:
                            mid_meta[r['mid']] = (r['mname'] or str(r['mid']), r['unit'])
                    mid_header = {
                        mid: f"{name} ({unit})" if unit else name
                        for mid, (name, unit) in mid_meta.items()
                    }

                    # Pivoted sheet: interleaved Value / Unc columns per parameter
                    idx_cols = ['analysisid', 'samplecode', 'sname']
                    piv_val = df_res.pivot_table(index=idx_cols, columns='mid',
                                                 values='fvalue', aggfunc='first').reset_index()
                    piv_unc = df_res.pivot_table(index=idx_cols, columns='mid',
                                                 values='fvalueunc', aggfunc='first').reset_index()
                    piv_val.columns.name = None
                    piv_unc.columns.name = None

                    val_rename = {mid: mid_header[mid] for mid in mid_meta}
                    unc_rename = {mid: f"{mid_header[mid]} Unc" for mid in mid_meta}
                    piv_val.rename(columns={**val_rename, 'analysisid': 'Analysis ID',
                                            'samplecode': 'Sample ID', 'sname': 'Name'}, inplace=True)
                    piv_unc.rename(columns={**unc_rename, 'analysisid': 'Analysis ID',
                                            'samplecode': 'Sample ID', 'sname': 'Name'}, inplace=True)

                    id_cols = ['Analysis ID', 'Sample ID', 'Name']
                    ordered_cols = id_cols.copy()
                    for mid in sorted(mid_meta.keys()):
                        ordered_cols.append(mid_header[mid])
                        ordered_cols.append(f"{mid_header[mid]} Unc")

                    pivot = piv_val.merge(
                        piv_unc.drop(columns=['Sample ID', 'Name']),
                        on='Analysis ID', how='left'
                    )
                    pivot = pivot[[c for c in ordered_cols if c in pivot.columns]]
                    pivot.to_excel(xw, sheet_name="Results (Pivoted)", index=False)

            QMessageBox.information(self, "Exported", f"Saved to:\n{path}")

        except Exception as e:
            logging.error(f"SIAM export failed: {e}", exc_info=True)
            QMessageBox.critical(self, "Export Failed", str(e))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = SiamRunDetailsWindow(10002)
    w.show()
    sys.exit(app.exec_())
