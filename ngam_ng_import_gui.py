"""
ngam_ng_import_gui.py
=====================
Interactive Noble Gas sequence data import dialog.

Step workflow:
  1. Browse folder → select sequence folder
  2. Parse         → read XLSM files, populate Pipeline tab and signal charts
  3. Process       → match to NGPreparations load list, populate Results tab
                     and final results charts
  4. Save          → write to ngam.ngsequenceresults, close
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QSplitter, QFileDialog, QFrame,
    QMessageBox, QSizePolicy, QTabWidget, QWidget, QTextEdit,
    QApplication, QComboBox,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from db_core import db_manager
from sqlalchemy import text
from shared_utils import get_current_user_id, normalize_login_name
from gui_utils import show_message

from ngam_ng_parser import parse_ng_sequence, NGSequenceParsed
from ngam_ng_processor import process_ng_run, NGRunResult, NGPreparationRecord

log = logging.getLogger(__name__)

# ─────────────────────────────── palette ──────────────────────────────────────
_ACCENT = "#2E7D32"   # green accent for NG

_HDR_SS = (
    "QHeaderView::section {"
    "  background-color:#2E7D32; color:white; font-weight:bold;"
    "  padding:3px 6px; border:none; border-right:1px solid #1B5E20; }"
    "QHeaderView::section:last-section { border-right:none; }"
)
_TABLE_SS = (
    "QTableWidget { border:1px solid #C5CDD9; background:white; }"
    "QTableWidget::item { padding:3px 5px; }"
    "QTableWidget::item:selected { background:#C8E6C9; color:#000; }"
)
_GBX_SS = (
    "QGroupBox { background:#F5F7FA; border:1px solid #C5CDD9; border-radius:4px;"
    "  margin-top:8px; }"
    "QGroupBox::title { subcontrol-origin:margin; left:8px; padding:0 4px; }"
)
_BTN_STEP = (
    "QPushButton {{ background:{bg}; color:white; font-weight:bold; border:none;"
    " padding:6px 8px; border-radius:4px; text-align:left; }}"
    "QPushButton:hover {{ background:{hov}; }}"
    "QPushButton:disabled {{ background:#B0BEC5; color:#78909C; }}"
)
_BTN_GRAY = (
    "QPushButton { background:#7F8C8D; color:white; font-weight:bold; border:none;"
    " padding:4px 12px; border-radius:4px; }"
    "QPushButton:hover { background:#636E72; }"
)

_COL_BLANK    = QColor("#BBDEFB")   # blue
_COL_STANDARD = QColor("#C8E6C9")   # green
_COL_AIR      = QColor("#FFF3E0")   # amber
_COL_SAMPLE   = QColor("#FFFFFF")   # white
_COL_INVALID  = QColor("#FFCDD2")   # red-ish

_CHART_COLS = {
    "blank":    "#64B5F6",
    "standard": "#81C784",
    "air_ref":  "#FFB74D",
    "sample":   "#90A4AE",
    "invalid":  "#EF9A9A",
}


# ─────────────────────────────── small helpers ────────────────────────────────

def _pev():
    QApplication.processEvents()


def _fmt(val: Optional[float], sig: int = 4) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return ""
    return f"{val:.{sig}g}"


def _fmt_sci(val: Optional[float]) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return ""
    return f"{val:.3e}"


def _row_color(ptype: str) -> QColor:
    return {
        "blank":    _COL_BLANK,
        "standard": _COL_STANDARD,
        "air_ref":  _COL_AIR,
        "sample":   _COL_SAMPLE,
        "invalid":  _COL_INVALID,
    }.get(ptype, _COL_SAMPLE)


def _make_item(txt: str, color: Optional[QColor] = None,
               bold: bool = False, align: int = Qt.AlignLeft) -> QTableWidgetItem:
    item = QTableWidgetItem(str(txt))
    item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
    item.setTextAlignment(align | Qt.AlignVCenter)
    if color:
        item.setBackground(color)
    if bold:
        f = item.font()
        f.setBold(True)
        item.setFont(f)
    return item


def _clear_figure(canvas: FigureCanvas):
    canvas.figure.clear()
    canvas.draw_idle()


# ─────────────────────────────── dialog ───────────────────────────────────────

class NGAMNGImportDialog(QDialog):
    """Interactive import dialog for Noble Gas sequence data."""

    def __init__(self, run_id: int, datapath: str = "", parent=None):
        super().__init__(parent)
        self._run_id  = run_id
        self._parsed: Optional[NGSequenceParsed] = None
        self._result: Optional[NGRunResult]      = None
        self._preparations: List[NGPreparationRecord] = []
        self._cur_inlet = 0   # 0-based index into _parsed.pipeline

        self.setWindowTitle(f"Import Noble Gas MS Data — Run #{run_id}")
        self.resize(1380, 840)
        self.setMinimumSize(1000, 640)

        self._build_ui()

        if datapath:
            self.txtFolder.setText(datapath)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 4)
        root.setSpacing(4)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(5)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([280, 1100])
        splitter.setStretchFactor(1, 1)

        root.addWidget(splitter, 1)
        root.addWidget(self._build_status_bar())

    # ── left panel ────────────────────────────────────────────────────────────

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(284)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)

        lay.addWidget(self._build_folder_group())
        lay.addWidget(self._build_options_group())
        lay.addWidget(self._build_steps_group())
        lay.addWidget(self._build_summary_group())
        lay.addStretch()
        return w

    def _build_folder_group(self) -> QGroupBox:
        grp = QGroupBox("Data Folder")
        grp.setStyleSheet(_GBX_SS)
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(6, 12, 6, 6)
        lay.setSpacing(4)

        self.txtFolder = QLineEdit()
        self.txtFolder.setReadOnly(True)
        self.txtFolder.setPlaceholderText("Browse to a sequence folder…")
        self.txtFolder.setStyleSheet("background:white;")

        btn_browse = QPushButton("Browse…")
        btn_browse.setStyleSheet(_BTN_GRAY)
        btn_browse.setFixedHeight(26)
        btn_browse.clicked.connect(self._browse_folder)

        lay.addWidget(self.txtFolder)
        lay.addWidget(btn_browse)

        lbl_run = QLabel(f"Run:  #{self._run_id}")
        f = lbl_run.font()
        f.setBold(True)
        f.setPointSize(10)
        lbl_run.setFont(f)
        lbl_run.setStyleSheet(f"color:{_ACCENT};")
        lay.addWidget(lbl_run)
        return grp

    def _build_options_group(self) -> QGroupBox:
        grp = QGroupBox("Options")
        grp.setStyleSheet(_GBX_SS)
        lay = QGridLayout(grp)
        lay.setContentsMargins(6, 12, 6, 6)
        lay.setHorizontalSpacing(6)
        lay.setVerticalSpacing(4)

        def lbl(t):
            l = QLabel(t)
            l.setStyleSheet("color:#555; font-size:10px;")
            return l

        self.cmbIsotope = QComboBox()
        self.cmbIsotope.addItem("— select after parse —")
        self.cmbIsotope.currentIndexChanged.connect(self._on_isotope_changed)
        lay.addWidget(lbl("Isotope:"), 0, 0)
        lay.addWidget(self.cmbIsotope, 0, 1)

        self.cmbStage = QComboBox()
        for stage in ["Blank Corrected", "Repro Corrected", "Lin Corrected"]:
            self.cmbStage.addItem(stage)
        self.cmbStage.currentIndexChanged.connect(self._on_stage_changed)
        lay.addWidget(lbl("Display:"), 1, 0)
        lay.addWidget(self.cmbStage, 1, 1)

        return grp

    def _build_steps_group(self) -> QGroupBox:
        grp = QGroupBox("Steps")
        grp.setStyleSheet(_GBX_SS)
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(8, 14, 8, 8)
        lay.setSpacing(6)

        def _step_btn(label, bg, hov):
            b = QPushButton(label)
            b.setStyleSheet(_BTN_STEP.format(bg=bg, hov=hov))
            b.setFixedHeight(32)
            return b

        self.btnParse   = _step_btn("1.  Parse Folder",  "#546E7A", "#37474F")
        self.btnProcess = _step_btn("2.  Process",        "#7B68EE", "#6A5ACD")
        self.btnSave    = _step_btn("3.  Save to DB",     "#27AE60", "#1E8449")
        btn_close       = _step_btn("      Close",         "#7F8C8D", "#636E72")

        self.btnProcess.setEnabled(False)
        self.btnSave.setEnabled(False)

        self.btnParse.clicked.connect(self._do_parse)
        self.btnProcess.clicked.connect(self._do_process)
        self.btnSave.clicked.connect(self._do_save)
        btn_close.clicked.connect(self.reject)

        lay.addWidget(self.btnParse)
        lay.addWidget(self.btnProcess)
        lay.addWidget(self.btnSave)
        lay.addSpacing(4)
        lay.addWidget(btn_close)
        return grp

    def _build_summary_group(self) -> QGroupBox:
        grp = QGroupBox("Summary")
        grp.setStyleSheet(_GBX_SS)
        lay = QGridLayout(grp)
        lay.setContentsMargins(8, 14, 8, 8)
        lay.setHorizontalSpacing(6)
        lay.setVerticalSpacing(4)

        def _cap(txt):
            l = QLabel(txt)
            l.setStyleSheet("color:#555; font-size:10px;")
            return l

        def _val():
            l = QLabel("—")
            l.setStyleSheet("font-weight:bold; font-size:10px;")
            return l

        self.lblSumInlets    = _val()
        self.lblSumBlanks    = _val()
        self.lblSumStds      = _val()
        self.lblSumAirRefs   = _val()
        self.lblSumSamples   = _val()
        self.lblSumIsotopes  = _val()
        self.lblSumResults   = _val()

        rows = [
            ("Inlets:",        self.lblSumInlets),
            ("Blanks:",        self.lblSumBlanks),
            ("Standards:",     self.lblSumStds),
            ("Air Refs:",      self.lblSumAirRefs),
            ("Samples:",       self.lblSumSamples),
            ("Isotope files:", self.lblSumIsotopes),
            ("Final Results:", self.lblSumResults),
        ]
        for i, (cap, widget) in enumerate(rows):
            lay.addWidget(_cap(cap),  i, 0)
            lay.addWidget(widget,     i, 1)
        return grp

    # ── right panel ────────────────────────────────────────────────────────────

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        vsplit = QSplitter(Qt.Vertical)
        vsplit.setHandleWidth(5)
        vsplit.addWidget(self._build_chart_panel())
        vsplit.addWidget(self._build_data_tabs())
        vsplit.setSizes([320, 440])
        vsplit.setStretchFactor(1, 1)

        lay.addWidget(vsplit)
        return w

    def _build_chart_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 2)
        lay.setSpacing(2)

        self.tabsCharts = QTabWidget()
        self.tabsCharts.setStyleSheet(
            "QTabWidget::pane { border:1px solid #C5CDD9; }"
            "QTabBar::tab { padding:4px 14px; font-size:10px; }"
            f"QTabBar::tab:selected {{ background:{_ACCENT}; color:white; }}"
        )

        self._canvas_he      = FigureCanvas(Figure(figsize=(5, 2.6), tight_layout=True))
        self._canvas_conc    = FigureCanvas(Figure(figsize=(5, 2.6), tight_layout=True))
        self._canvas_qc      = FigureCanvas(Figure(figsize=(5, 2.6), tight_layout=True))
        self._canvas_blank_fit  = FigureCanvas(Figure(figsize=(5, 2.6), tight_layout=True))
        self._canvas_drift      = FigureCanvas(Figure(figsize=(5, 2.6), tight_layout=True))
        self._canvas_linearity  = FigureCanvas(Figure(figsize=(5, 2.6), tight_layout=True))
        self._canvas_cal_blank  = FigureCanvas(Figure(figsize=(5, 2.6), tight_layout=True))
        self._canvas_cal_lin    = FigureCanvas(Figure(figsize=(5, 2.6), tight_layout=True))
        self._canvas_signal_ts  = FigureCanvas(Figure(figsize=(5, 2.6), tight_layout=True))

        for canvas, title in [
            (self._canvas_he,   "He Signals"),
            (self._canvas_conc, "Final Concentrations"),
            (self._canvas_qc,   "QC Chart"),
            (self._canvas_blank_fit,  "Blank Fit"),
            (self._canvas_drift,      "Drift Correction"),
            (self._canvas_linearity,  "Linearity"),
            (self._canvas_cal_blank,  "Blank→ccSTP"),
            (self._canvas_cal_lin,    "Lin→ccSTP"),
            (self._canvas_signal_ts,  "Signal Progression"),
        ]:
            canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.tabsCharts.addTab(canvas, title)

        self.tabsCharts.currentChanged.connect(self._on_chart_tab_changed)

        lay.addWidget(self.tabsCharts, 1)

        # Navigation row
        nav = QHBoxLayout()
        nav.setSpacing(6)
        self.btnPrev = QPushButton("◀ Prev")
        self.btnNext = QPushButton("Next ▶")
        self.btnPrev.setFixedHeight(24)
        self.btnNext.setFixedHeight(24)
        self.btnPrev.setStyleSheet(_BTN_GRAY)
        self.btnNext.setStyleSheet(_BTN_GRAY)
        self.btnPrev.setEnabled(False)
        self.btnNext.setEnabled(False)
        self.btnPrev.clicked.connect(self._prev_inlet)
        self.btnNext.clicked.connect(self._next_inlet)

        self.lblInletNav = QLabel("—")
        self.lblInletNav.setAlignment(Qt.AlignCenter)
        self.lblInletNav.setStyleSheet("font-weight:bold; color:#333; font-size:10px;")

        nav.addWidget(self.btnPrev)
        nav.addStretch()
        nav.addWidget(self.lblInletNav)
        nav.addStretch()
        nav.addWidget(self.btnNext)
        lay.addLayout(nav)

        return w

    def _build_data_tabs(self) -> QTabWidget:
        self.tabsData = QTabWidget()
        self.tabsData.setStyleSheet(
            "QTabWidget::pane { border:1px solid #C5CDD9; }"
            "QTabBar::tab { padding:4px 16px; font-size:10px; }"
            f"QTabBar::tab:selected {{ background:{_ACCENT}; color:white; }}"
        )

        # ── Tab 1: Pipeline ──────────────────────────────────────────────────
        _PIPE_COLS = ["InletNum", "Name", "Type", "Port", "SampleID", "SubSampleID", "SizeHint"]
        self.tblPipeline = QTableWidget(0, len(_PIPE_COLS))
        self.tblPipeline.setHorizontalHeaderLabels(_PIPE_COLS)
        self.tblPipeline.setStyleSheet(_TABLE_SS + _HDR_SS)
        self.tblPipeline.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblPipeline.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblPipeline.verticalHeader().setVisible(False)
        self.tblPipeline.currentCellChanged.connect(self._on_pipeline_row_changed)
        self.tabsData.addTab(self.tblPipeline, "Pipeline")

        # ── Tab 2: Isotope Signals ───────────────────────────────────────────
        _ISO_COLS = ["Inlet", "Type", "He", "4He", "3He", "Ne", "20Ne", "22Ne",
                     "Ar", "36Ar", "40Ar", "84Kr", "136Xe"]
        self.tblSignals = QTableWidget(0, len(_ISO_COLS))
        self.tblSignals.setHorizontalHeaderLabels(_ISO_COLS)
        self.tblSignals.setStyleSheet(_TABLE_SS + _HDR_SS)
        self.tblSignals.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblSignals.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblSignals.verticalHeader().setVisible(False)
        self.tabsData.addTab(self.tblSignals, "Isotope Signals")

        # ── Tab 3: Final Results ─────────────────────────────────────────────
        _FR_COLS = [
            "SRG",
            "He (cm³/g)", "±He", "He%",
            "Ne (cm³/g)", "±Ne", "Ne%",
            "Ar (cm³/g)", "±Ar", "Ar%",
            "Kr (cm³/g)", "±Kr", "Kr%",
            "Xe (cm³/g)", "±Xe", "Xe%",
            "NGT (°C)", "ExcessAir", "χ²",
        ]
        self.tblFinalResults = QTableWidget(0, len(_FR_COLS))
        self.tblFinalResults.setHorizontalHeaderLabels(_FR_COLS)
        self.tblFinalResults.setStyleSheet(_TABLE_SS + _HDR_SS)
        self.tblFinalResults.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblFinalResults.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblFinalResults.verticalHeader().setVisible(False)
        self.tabsData.addTab(self.tblFinalResults, "Final Results")

        # ── Tab 4: QA/QC Report ──────────────────────────────────────────────
        self.txtQAQC = QTextEdit()
        self.txtQAQC.setReadOnly(True)
        self.txtQAQC.setStyleSheet("background:white; font-size:11px;")
        self.tabsData.addTab(self.txtQAQC, "QA/QC Report")

        return self.tabsData

    # ── status bar ─────────────────────────────────────────────────────────────

    def _build_status_bar(self) -> QFrame:
        bar = QFrame()
        bar.setStyleSheet("background:#EAF0F6; border-top:1px solid #AAB8CC;")
        bar.setFixedHeight(28)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 3, 8, 3)
        self.lblStatus = QLabel("Ready.  Browse to a sequence folder, then click Parse.")
        self.lblStatus.setStyleSheet("color:#555; font-size:10px;")
        lay.addWidget(self.lblStatus)
        return bar

    # ── helpers ────────────────────────────────────────────────────────────────

    def _set_status(self, msg: str, color: str = "#333"):
        self.lblStatus.setText(msg)
        self.lblStatus.setStyleSheet(f"color:{color}; font-size:10px;")
        _pev()

    def _update_summary(self):
        if self._parsed:
            p = self._parsed
            self.lblSumInlets.setText(str(p.n_inlets))
            self.lblSumBlanks.setText(str(p.n_blanks))
            self.lblSumStds.setText(str(p.n_standards))
            self.lblSumAirRefs.setText(
                str(sum(1 for e in p.pipeline if e.position_type == "air_ref"))
            )
            self.lblSumSamples.setText(str(p.n_samples))
            self.lblSumIsotopes.setText(str(len(p.isotope_signals)))
            self.lblSumResults.setText(str(len(p.final_results)))

        if self._result:
            r = self._result
            self.lblSumBlanks.setText(str(r.n_blanks))
            self.lblSumStds.setText(str(r.n_standards))
            self.lblSumAirRefs.setText(str(r.n_air_refs))
            self.lblSumSamples.setText(str(r.n_samples))

    def _update_nav(self):
        if not self._parsed or not self._parsed.pipeline:
            self.lblInletNav.setText("—")
            self.btnPrev.setEnabled(False)
            self.btnNext.setEnabled(False)
            return
        n = len(self._parsed.pipeline)
        entry = self._parsed.pipeline[self._cur_inlet]
        self.lblInletNav.setText(
            f"Inlet {self._cur_inlet + 1} / {n}  —  "
            f"{entry.name[:40]}  [{entry.position_type}]"
        )
        self.btnPrev.setEnabled(self._cur_inlet > 0)
        self.btnNext.setEnabled(self._cur_inlet < n - 1)

    def _prev_inlet(self):
        if self._parsed and self._cur_inlet > 0:
            self._cur_inlet -= 1
            self._update_nav()
            self._sync_pipeline_selection()
            self._draw_he_signals()
            self._redraw_isotope_charts()

    def _next_inlet(self):
        if self._parsed and self._cur_inlet < len(self._parsed.pipeline) - 1:
            self._cur_inlet += 1
            self._update_nav()
            self._sync_pipeline_selection()
            self._draw_he_signals()
            self._redraw_isotope_charts()

    def _sync_pipeline_selection(self):
        """Keep the Pipeline table row in sync with _cur_inlet (no signal loop)."""
        tbl = self.tblPipeline
        tbl.blockSignals(True)
        tbl.selectRow(self._cur_inlet)
        tbl.scrollTo(tbl.model().index(self._cur_inlet, 0))
        tbl.blockSignals(False)

    def _on_pipeline_row_changed(self, row, _col, _prev_row, _prev_col):
        if row < 0 or not self._parsed:
            return
        self._cur_inlet = row
        self._update_nav()
        self._draw_he_signals()
        self._redraw_isotope_charts()

    # ── actions ────────────────────────────────────────────────────────────────

    def _browse_folder(self):
        start = self.txtFolder.text().strip() or os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(
            self, "Select NG Sequence Folder", start
        )
        if path:
            self.txtFolder.setText(path)

    def _do_parse(self):
        folder = self.txtFolder.text().strip()
        if not folder:
            show_message(self, "No Folder", "Browse to a sequence folder first.", QMessageBox.Warning)
            return
        if not os.path.isdir(folder):
            show_message(self, "Not Found", f"Folder not found:\n{folder}", QMessageBox.Warning)
            return

        self._set_status("Parsing XLSM files…")
        try:
            self._parsed = parse_ng_sequence(folder)
        except Exception as exc:
            log.exception("NG parse failed")
            show_message(self, "Parse Error", str(exc), QMessageBox.Critical)
            self._set_status("Parse failed.", "#C0392B")
            return

        # Reset downstream
        self._result = None
        self._preparations = []
        self._cur_inlet = 0

        self._populate_pipeline_table()
        self._populate_signals_table()
        self._populate_final_results_table()
        self._update_summary()
        self._update_nav()
        self._populate_isotope_combo()
        self._draw_he_signals()
        _clear_figure(self._canvas_conc)
        _clear_figure(self._canvas_qc)
        self.txtQAQC.clear()
        self.tabsData.setCurrentIndex(0)
        self.tabsCharts.setCurrentIndex(0)

        self.btnProcess.setEnabled(True)
        self.btnSave.setEnabled(False)

        p = self._parsed
        self._set_status(
            f"Parsed: {p.n_inlets} inlets, {p.n_samples} samples, "
            f"{p.n_blanks} blanks, {len(p.isotope_signals)} isotope files, "
            f"{len(p.final_results)} final results.  "
            "Click Process to match to load list.",
            "#1A5276",
        )

    def _do_process(self):
        if self._parsed is None:
            return

        self._set_status("Fetching preparations from database…")
        try:
            self._preparations = self._fetch_preparations(self._run_id)
        except Exception as exc:
            log.exception("Preparations fetch failed")
            show_message(self, "DB Error", str(exc), QMessageBox.Critical)
            self._set_status("Could not fetch preparations.", "#C0392B")
            return

        self._set_status("Processing…")
        try:
            self._result = process_ng_run(
                self._parsed, self._preparations, run_id=self._run_id
            )
        except Exception as exc:
            log.exception("NG processing failed")
            show_message(self, "Processing Error", str(exc), QMessageBox.Critical)
            self._set_status("Processing failed.", "#C0392B")
            return

        self._update_summary()
        self._draw_he_signals()
        self._draw_concentrations()
        self._draw_qc_chart()
        self._draw_blank_fit()
        self._draw_drift()
        self._draw_linearity()
        self._draw_signal_progression()
        self._draw_cal_blank()
        self._draw_cal_lin()
        self.txtQAQC.setHtml(self._build_qaqc_html())
        self.tabsCharts.setCurrentIndex(0)

        self.btnSave.setEnabled(True)
        r = self._result
        self._set_status(
            f"Done.  {r.n_blanks} blanks | {r.n_standards} standards | "
            f"{r.n_air_refs} air refs | {r.n_samples} samples | "
            f"{len(r.unmatched_final_results)} unmatched results.  "
            "Review, then Save.",
            "#1B6020",
        )

    def _do_save(self):
        if self._parsed is None or self._result is None:
            return

        try:
            user = normalize_login_name(get_current_user_id())
        except Exception:
            user = "unknown"

        now    = datetime.now()
        folder = self.txtFolder.text().strip()
        r      = self._result

        try:
            with db_manager.get_connection() as conn:
                # Create table
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS ngam.ngsequenceresults (
                        resultid              SERIAL PRIMARY KEY,
                        runid                 INTEGER NOT NULL,
                        inoblepreparationid   INTEGER,
                        positioninrun         INTEGER,
                        iportnumber           INTEGER,
                        analysisid            INTEGER,
                        position_type         VARCHAR(20),
                        srg                   VARCHAR(100),
                        he_ccstp_g            DOUBLE PRECISION,
                        he_err                DOUBLE PRECISION,
                        he_err_pct            DOUBLE PRECISION,
                        ne_ccstp_g            DOUBLE PRECISION,
                        ne_err                DOUBLE PRECISION,
                        ne_err_pct            DOUBLE PRECISION,
                        ar_ccstp_g            DOUBLE PRECISION,
                        ar_err                DOUBLE PRECISION,
                        ar_err_pct            DOUBLE PRECISION,
                        kr_ccstp_g            DOUBLE PRECISION,
                        kr_err                DOUBLE PRECISION,
                        kr_err_pct            DOUBLE PRECISION,
                        xe_ccstp_g            DOUBLE PRECISION,
                        xe_err                DOUBLE PRECISION,
                        xe_err_pct            DOUBLE PRECISION,
                        ngt                   DOUBLE PRECISION,
                        excess_air            DOUBLE PRECISION,
                        chi_square            DOUBLE PRECISION,
                        he_blank_corrected    DOUBLE PRECISION,
                        he4_blank_corrected   DOUBLE PRECISION,
                        ne_blank_corrected    DOUBLE PRECISION,
                        ar_blank_corrected    DOUBLE PRECISION,
                        isblank               BOOLEAN DEFAULT FALSE,
                        isstandard            BOOLEAN DEFAULT FALSE,
                        isrejected            BOOLEAN DEFAULT FALSE,
                        createdatestamp       TIMESTAMP,
                        createuserstamp       VARCHAR(100),
                        UNIQUE(runid, positioninrun)
                    )
                """))

                for ir in r.inlet_results:
                    fr = ir.final_result

                    # Pull blank-corrected signals from isotope_signals
                    def _bc(iso_name: str) -> Optional[float]:
                        sig = ir.isotope_signals.get(iso_name)
                        return sig.blank_corrected if sig else None

                    conn.execute(text("""
                        INSERT INTO ngam.ngsequenceresults (
                            runid, inoblepreparationid, positioninrun,
                            iportnumber, analysisid, position_type, srg,
                            he_ccstp_g,  he_err,  he_err_pct,
                            ne_ccstp_g,  ne_err,  ne_err_pct,
                            ar_ccstp_g,  ar_err,  ar_err_pct,
                            kr_ccstp_g,  kr_err,  kr_err_pct,
                            xe_ccstp_g,  xe_err,  xe_err_pct,
                            ngt, excess_air, chi_square,
                            he_blank_corrected, he4_blank_corrected,
                            ne_blank_corrected, ar_blank_corrected,
                            isblank, isstandard, isrejected,
                            createdatestamp, createuserstamp
                        ) VALUES (
                            :seq_id, :prep_id, :inlet, :port, :aid, :ptype, :srg,
                            :he_c,  :he_e,  :he_ep,
                            :ne_c,  :ne_e,  :ne_ep,
                            :ar_c,  :ar_e,  :ar_ep,
                            :kr_c,  :kr_e,  :kr_ep,
                            :xe_c,  :xe_e,  :xe_ep,
                            :ngt, :exair, :chisq,
                            :he_bc, :he4_bc, :ne_bc, :ar_bc,
                            :isblank, :isstd, :isrej,
                            :created, :usr
                        )
                        ON CONFLICT (runid, positioninrun) DO UPDATE SET
                            inoblepreparationid  = EXCLUDED.inoblepreparationid,
                            iportnumber          = EXCLUDED.iportnumber,
                            analysisid           = EXCLUDED.analysisid,
                            position_type        = EXCLUDED.position_type,
                            srg                  = EXCLUDED.srg,
                            he_ccstp_g           = EXCLUDED.he_ccstp_g,
                            he_err               = EXCLUDED.he_err,
                            he_err_pct           = EXCLUDED.he_err_pct,
                            ne_ccstp_g           = EXCLUDED.ne_ccstp_g,
                            ne_err               = EXCLUDED.ne_err,
                            ne_err_pct           = EXCLUDED.ne_err_pct,
                            ar_ccstp_g           = EXCLUDED.ar_ccstp_g,
                            ar_err               = EXCLUDED.ar_err,
                            ar_err_pct           = EXCLUDED.ar_err_pct,
                            kr_ccstp_g           = EXCLUDED.kr_ccstp_g,
                            kr_err               = EXCLUDED.kr_err,
                            kr_err_pct           = EXCLUDED.kr_err_pct,
                            xe_ccstp_g           = EXCLUDED.xe_ccstp_g,
                            xe_err               = EXCLUDED.xe_err,
                            xe_err_pct           = EXCLUDED.xe_err_pct,
                            ngt                  = EXCLUDED.ngt,
                            excess_air           = EXCLUDED.excess_air,
                            chi_square           = EXCLUDED.chi_square,
                            he_blank_corrected   = EXCLUDED.he_blank_corrected,
                            he4_blank_corrected  = EXCLUDED.he4_blank_corrected,
                            ne_blank_corrected   = EXCLUDED.ne_blank_corrected,
                            ar_blank_corrected   = EXCLUDED.ar_blank_corrected,
                            isblank              = EXCLUDED.isblank,
                            isstandard           = EXCLUDED.isstandard,
                            isrejected           = EXCLUDED.isrejected,
                            createdatestamp      = EXCLUDED.createdatestamp,
                            createuserstamp      = EXCLUDED.createuserstamp
                    """), {
                        "seq_id":  self._run_id,
                        "prep_id": ir.preparation_id,
                        "inlet":   ir.inlet_num,
                        "port":    ir.port_num,
                        "aid":     ir.analysis_id,
                        "ptype":   ir.position_type,
                        "srg":     fr.srg if fr else None,
                        "he_c":    fr.he_ccstp_g  if fr else None,
                        "he_e":    fr.he_err       if fr else None,
                        "he_ep":   fr.he_err_pct   if fr else None,
                        "ne_c":    fr.ne_ccstp_g   if fr else None,
                        "ne_e":    fr.ne_err        if fr else None,
                        "ne_ep":   fr.ne_err_pct    if fr else None,
                        "ar_c":    fr.ar_ccstp_g    if fr else None,
                        "ar_e":    fr.ar_err         if fr else None,
                        "ar_ep":   fr.ar_err_pct     if fr else None,
                        "kr_c":    fr.kr_ccstp_g    if fr else None,
                        "kr_e":    fr.kr_err         if fr else None,
                        "kr_ep":   fr.kr_err_pct     if fr else None,
                        "xe_c":    fr.xe_ccstp_g    if fr else None,
                        "xe_e":    fr.xe_err         if fr else None,
                        "xe_ep":   fr.xe_err_pct     if fr else None,
                        "ngt":     fr.ngt            if fr else None,
                        "exair":   fr.excess_air     if fr else None,
                        "chisq":   fr.chi_square     if fr else None,
                        "he_bc":   _bc("He"),
                        "he4_bc":  _bc("4He"),
                        "ne_bc":   _bc("Ne"),
                        "ar_bc":   _bc("Ar"),
                        "isblank": ir.position_type == "blank",
                        "isstd":   ir.position_type == "standard",
                        "isrej":   False,
                        "created": now,
                        "usr":     user,
                    })

                # Update datapath on ngsequencerun (graceful if column missing)
                try:
                    conn.execute(text("""
                        UPDATE ngam.ngsequencerun
                        SET datapath = :path
                        WHERE runid = :rid
                    """), {"path": folder, "rid": self._run_id})
                except Exception as exc2:
                    log.warning("Could not update datapath: %s", exc2)

                conn.commit()

        except Exception as exc:
            log.exception("Save to DB failed")
            show_message(self, "Save Error", str(exc), QMessageBox.Critical)
            self._set_status("Save failed.", "#C0392B")
            return

        # ── EQW correction-factor save (non-blocking; failures are logged only) ──
        self._save_eqw_runs(r, user)

        self._set_status(
            f"Saved {len(r.inlet_results)} inlet results for run #{self._run_id}.",
            "#1B6020",
        )
        QMessageBox.information(
            self, "Saved",
            f"Saved {len(r.inlet_results)} records to ngam.ngsequenceresults."
        )
        self.accept()

    def _save_eqw_runs(self, result, user: str) -> None:
        """After normal save, persist EQW inlets (sampletype=50) to ng_eqw_run."""
        from ngam_eqw_processor import extract_eqw_cf_data, save_eqw_run

        eqw_inlets = [ir for ir in result.inlet_results if ir.inlet_type == "sample"]
        if not eqw_inlets:
            return

        # Look up which analysis IDs belong to sampletype=50
        try:
            with db_manager.get_connection() as conn:
                aid_list = [ir.analysis_id for ir in eqw_inlets if ir.analysis_id]
                if not aid_list:
                    return
                rows = conn.execute(text("""
                    SELECT a.analysisid,
                           COALESCE(s.container_type, 1) AS container_type_id,
                           s.sampletype
                    FROM   public.analysis a
                    JOIN   public.sample   s ON s.sampleid = a.sampleid
                                            AND s.prefix   = a.prefix
                    WHERE  a.analysisid = ANY(:aids)
                      AND  s.sampletype = 50
                """), {"aids": aid_list}).fetchall()

                if not rows:
                    return

                eqw_meta = {int(r[0]): int(r[1]) for r in rows}

                # Fetch equipment ID from the sequence run
                eq_row = conn.execute(text("""
                    SELECT equipmentid FROM ngam.ngsequencerun
                    WHERE  runid = :rid
                """), {"rid": self._run_id}).fetchone()
                equipment_id = int(eq_row[0]) if eq_row and eq_row[0] else None

                # Fetch per-sample T/P/S from ngextractiondata
                tps_rows = conn.execute(text("""
                    SELECT ed.analysisid,
                           ed.temperature_c,
                           ed.salinity_ppt,
                           ed.lab_pressure_torr
                    FROM   ngam.ngextractiondata ed
                    WHERE  ed.analysisid = ANY(:aids)
                      AND  ed.isignored IS DISTINCT FROM 1
                """), {"aids": aid_list}).fetchall()
                tps = {int(r[0]): (r[1], r[2], r[3]) for r in tps_rows}

                saved = 0
                for ir in eqw_inlets:
                    aid = ir.analysis_id
                    if aid not in eqw_meta:
                        continue
                    cf_data = extract_eqw_cf_data(ir)
                    if cf_data is None:
                        log.warning("EQW inlet aid=%s: Step 11 data absent — CF not saved", aid)
                        continue
                    t, s, p = tps.get(aid, (None, 0.0, None))
                    eid = save_eqw_run(
                        conn,
                        analysisid=aid,
                        container_type_id=eqw_meta[aid],
                        equipmentid=equipment_id,
                        temperature_c=float(t) if t is not None else None,
                        pressure_torr=float(p) if p is not None else None,
                        salinity_ppt=float(s) if s is not None else 0.0,
                        cf_data=cf_data,
                        created_by=user,
                    )
                    if eid:
                        saved += 1

                if saved:
                    conn.commit()
                    log.info("Saved %d EQW CF run(s) for sequence %s", saved, self._run_id)

        except Exception as exc:
            log.error("EQW CF save failed (main results unaffected): %s", exc)

    # ── database operations ────────────────────────────────────────────────────

    def _fetch_preparations(self, run_id: int) -> List[NGPreparationRecord]:
        with db_manager.get_connection() as conn:
            rows = conn.execute(text("""
                SELECT p.inoblepreparationid, p.runid, p.positioninrun,
                       p.analysisid, p.iportnumber,
                       p.bisblank, p.bisreproreference, p.bislinreference,
                       p.nvcreferencegas, p.freferenceamount
                FROM ngam.ngpreparations p
                WHERE p.runid = :rid
                ORDER BY p.positioninrun
            """), {"rid": run_id}).fetchall()

        result = []
        for r in rows:
            r = tuple(r)
            result.append(NGPreparationRecord(
                inoblepreparationid=r[0],
                runid=r[1],
                positioninrun=r[2],
                analysisid=r[3],
                iportnumber=r[4],
                bisblank=bool(r[5]),
                bisreproreference=bool(r[6]),
                bislinreference=bool(r[7]),
                nvcreferences=r[8],
                freferenceamount=float(r[9]) if r[9] is not None else None,
            ))
        return result

    # ── table population ───────────────────────────────────────────────────────

    def _populate_isotope_combo(self):
        self.cmbIsotope.blockSignals(True)
        self.cmbIsotope.clear()
        if self._parsed and self._parsed.isotope_signals:
            for iso in sorted(self._parsed.isotope_signals.keys()):
                self.cmbIsotope.addItem(iso)
            # Default: prefer "He", "4He" or first
            for preferred in ("He", "4He", "20Ne", "36Ar"):
                idx = self.cmbIsotope.findText(preferred)
                if idx >= 0:
                    self.cmbIsotope.setCurrentIndex(idx)
                    break
        else:
            self.cmbIsotope.addItem("— select after parse —")
        self.cmbIsotope.blockSignals(False)

    def _populate_pipeline_table(self):
        if not self._parsed:
            return
        tbl = self.tblPipeline
        pipeline = self._parsed.pipeline
        tbl.setRowCount(len(pipeline))
        for row, entry in enumerate(pipeline):
            color = _row_color(entry.position_type)
            items = [
                _make_item(str(entry.inlet_num), color, align=Qt.AlignCenter),
                _make_item(entry.name, color),
                _make_item(entry.position_type, color),
                _make_item(str(entry.port_num) if entry.port_num is not None else "", color, align=Qt.AlignCenter),
                _make_item(str(entry.sample_id) if entry.sample_id is not None else "", color, align=Qt.AlignCenter),
                _make_item(entry.subsample_id or "", color),
                _make_item(_fmt(entry.size_hint), color, align=Qt.AlignRight),
            ]
            for col, item in enumerate(items):
                tbl.setItem(row, col, item)

        h = tbl.horizontalHeader()
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        for i in [0, 2, 3, 4, 5, 6]:
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)

    def _populate_signals_table(self):
        if not self._parsed:
            return
        pipeline = self._parsed.pipeline
        iso_by_inlet = self._build_iso_by_inlet()
        _ISO_KEYS = ["He", "4He", "3He", "Ne", "20Ne", "22Ne",
                     "Ar", "36Ar", "40Ar", "84Kr", "136Xe"]
        tbl = self.tblSignals
        tbl.setRowCount(len(pipeline))
        for row, entry in enumerate(pipeline):
            color = _row_color(entry.position_type)
            sigs = iso_by_inlet.get(entry.inlet_num, {})
            vals = [str(entry.inlet_num), entry.position_type]
            for iso in _ISO_KEYS:
                sig = sigs.get(iso)
                vals.append(_fmt_sci(self._stage_signal(sig)) if sig else "")
            for col, v in enumerate(vals):
                tbl.setItem(row, col, _make_item(v, color))

        h = tbl.horizontalHeader()
        for i in range(tbl.columnCount()):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)

    def _populate_final_results_table(self):
        if not self._parsed:
            return
        frs = self._parsed.final_results
        tbl = self.tblFinalResults
        tbl.setRowCount(len(frs))
        for row, fr in enumerate(frs):
            vals = [
                fr.srg,
                _fmt_sci(fr.he_ccstp_g),  _fmt_sci(fr.he_err),  _fmt(fr.he_err_pct, 3),
                _fmt_sci(fr.ne_ccstp_g),  _fmt_sci(fr.ne_err),  _fmt(fr.ne_err_pct, 3),
                _fmt_sci(fr.ar_ccstp_g),  _fmt_sci(fr.ar_err),  _fmt(fr.ar_err_pct, 3),
                _fmt_sci(fr.kr_ccstp_g),  _fmt_sci(fr.kr_err),  _fmt(fr.kr_err_pct, 3),
                _fmt_sci(fr.xe_ccstp_g),  _fmt_sci(fr.xe_err),  _fmt(fr.xe_err_pct, 3),
                _fmt(fr.ngt, 4),           _fmt_sci(fr.excess_air), _fmt(fr.chi_square, 4),
            ]
            for col, v in enumerate(vals):
                tbl.setItem(row, col, _make_item(v))

        h = tbl.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for i in range(1, tbl.columnCount()):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)

    # ── isotope helpers ────────────────────────────────────────────────────────

    def _build_iso_by_inlet(self) -> Dict[int, Dict[str, Any]]:
        if not self._parsed:
            return {}
        by_inlet: Dict[int, Dict[str, Any]] = {}
        for iso_name, signals in self._parsed.isotope_signals.items():
            for sig in signals:
                by_inlet.setdefault(sig.inlet_num, {})[iso_name] = sig
        return by_inlet

    # ── charts ────────────────────────────────────────────────────────────────

    def _draw_he_signals(self):
        """Bar chart of blank-corrected He signals per inlet, coloured by type."""
        canvas = self._canvas_he
        fig = canvas.figure
        fig.clear()

        if not self._parsed:
            canvas.draw_idle()
            return

        he_sigs = self._parsed.isotope_signals.get("He") or \
                  self._parsed.isotope_signals.get("4He")
        if not he_sigs:
            ax = fig.add_subplot(111)
            ax.text(0.5, 0.5, "He.xlsm not found", ha="center", va="center",
                    transform=ax.transAxes, fontsize=9, color="gray")
            canvas.draw_idle()
            return

        pipeline_map = {e.inlet_num: e for e in self._parsed.pipeline}
        inlets = [s.inlet_num for s in he_sigs]
        vals   = [s.blank_corrected or 0.0 for s in he_sigs]

        # Current inlet's inlet_num (for highlighting)
        cur_inlet_num = None
        if self._parsed.pipeline and self._cur_inlet < len(self._parsed.pipeline):
            cur_inlet_num = self._parsed.pipeline[self._cur_inlet].inlet_num

        edgecolors = []
        linewidths = []
        colors = []
        for s in he_sigs:
            e = pipeline_map.get(s.inlet_num)
            ptype = e.position_type if e else "invalid"
            base_color = _CHART_COLS.get(ptype, "#90A4AE")
            is_selected = (s.inlet_num == cur_inlet_num)
            colors.append(base_color)
            edgecolors.append("#000000" if is_selected else "none")
            linewidths.append(1.5 if is_selected else 0)

        ax = fig.add_subplot(111)
        ax.bar(range(len(inlets)), vals, color=colors,
               edgecolor=edgecolors, linewidth=linewidths)

        # Vertical line marking selected inlet
        if cur_inlet_num in inlets:
            sel_bar_idx = inlets.index(cur_inlet_num)
            ax.axvline(sel_bar_idx, color="#222", linewidth=0.8,
                       linestyle="--", alpha=0.6, zorder=5)

        ax.set_xticks(range(len(inlets)))
        ax.set_xticklabels([str(n) for n in inlets], fontsize=6, rotation=90)
        ax.set_xlabel("Inlet", fontsize=7)
        ax.set_ylabel("He blank-corrected", fontsize=7)

        sel_name = ""
        if cur_inlet_num is not None:
            e = pipeline_map.get(cur_inlet_num)
            if e:
                sel_name = f"  ▶ {e.name[:28]}"
        ax.set_title(f"He Signals by Inlet{sel_name}", fontsize=8)
        ax.tick_params(axis="y", labelsize=7)
        ax.grid(True, alpha=0.25, axis="y")
        fig.tight_layout()
        canvas.draw_idle()

    def _draw_concentrations(self):
        """Grouped bar chart for He, Ne, Ar concentrations (log scale)."""
        canvas = self._canvas_conc
        fig = canvas.figure
        fig.clear()

        if not self._parsed or not self._parsed.final_results:
            canvas.draw_idle()
            return

        frs = [fr for fr in self._parsed.final_results if fr.srg]
        if not frs:
            canvas.draw_idle()
            return

        srg_labels = [fr.srg[:12] for fr in frs]
        gases = ["He", "Ne", "Ar"]
        gas_vals = {
            "He": [fr.he_ccstp_g for fr in frs],
            "Ne": [fr.ne_ccstp_g for fr in frs],
            "Ar": [fr.ar_ccstp_g for fr in frs],
        }
        gas_colors = {"He": "#64B5F6", "Ne": "#81C784", "Ar": "#FFB74D"}

        axes = fig.subplots(1, 3, sharey=False)
        for ax, gas in zip(axes, gases):
            vals = gas_vals[gas]
            finite = [v for v in vals if v is not None and v > 0]
            color  = gas_colors[gas]
            x = range(len(frs))
            bar_vals = [v if (v is not None and v > 0) else float("nan")
                        for v in vals]
            ax.bar(x, bar_vals, color=color, edgecolor="none")
            if finite:
                ax.set_yscale("log")
            ax.set_xticks(list(x))
            ax.set_xticklabels(srg_labels, fontsize=5, rotation=80)
            ax.set_title(f"{gas} (cm³/g)", fontsize=7)
            ax.tick_params(axis="y", labelsize=6)
            ax.grid(True, alpha=0.25, axis="y")

        fig.suptitle("Final Concentrations", fontsize=8)
        fig.tight_layout()
        canvas.draw_idle()

    def _draw_qc_chart(self):
        """Scatter: inlet position vs He blank-corrected signal, coloured by type."""
        canvas = self._canvas_qc
        fig = canvas.figure
        fig.clear()

        if not self._parsed:
            canvas.draw_idle()
            return

        he_sigs = self._parsed.isotope_signals.get("He") or \
                  self._parsed.isotope_signals.get("4He")
        pipeline_map = {e.inlet_num: e for e in self._parsed.pipeline}

        ax = fig.add_subplot(111)
        if he_sigs:
            for sig in he_sigs:
                entry = pipeline_map.get(sig.inlet_num)
                ptype  = entry.position_type if entry else "invalid"
                color  = _CHART_COLS.get(ptype, "#90A4AE")
                val    = sig.blank_corrected or 0.0
                ax.scatter(sig.inlet_num, val, c=color, s=30, zorder=3)
        else:
            ax.text(0.5, 0.5, "No He signal data", ha="center", va="center",
                    transform=ax.transAxes, fontsize=9, color="gray")

        # Legend patches
        from matplotlib.patches import Patch
        legend_els = [
            Patch(facecolor=_CHART_COLS[t], label=t)
            for t in ["blank", "standard", "air_ref", "sample"]
        ]
        ax.legend(handles=legend_els, fontsize=6, loc="upper right")
        ax.set_xlabel("Inlet Number", fontsize=7)
        ax.set_ylabel("He blank-corrected", fontsize=7)
        ax.set_title("QC: Inlet Positions", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        canvas.draw_idle()

    # ── isotope chart helpers ──────────────────────────────────────────────────

    def _on_isotope_changed(self, _idx: int):
        self._redraw_isotope_charts()

    def _on_stage_changed(self, _idx: int):
        self._populate_signals_table()

    def _on_chart_tab_changed(self, idx: int):
        # Tabs 3-8 are isotope-specific
        if idx >= 3:
            self._redraw_isotope_charts()

    def _redraw_isotope_charts(self):
        if not self._parsed:
            return
        tab = self.tabsCharts.currentIndex()
        if tab == 3:
            self._draw_blank_fit()
        elif tab == 4:
            self._draw_drift()
        elif tab == 5:
            self._draw_linearity()
        elif tab == 6:
            self._draw_cal_blank()
        elif tab == 7:
            self._draw_cal_lin()
        elif tab == 8:
            self._draw_signal_progression()

    def _current_isotope_signals(self):
        """Return signals list for currently selected isotope, or None."""
        if not self._parsed:
            return None
        iso = self.cmbIsotope.currentText()
        return self._parsed.isotope_signals.get(iso)

    def _stage_signal(self, sig) -> Optional[float]:
        """Return the signal value for the currently selected correction stage."""
        stage = self.cmbStage.currentText()
        if stage == "Repro Corrected":
            return sig.repro_corrected
        elif stage == "Lin Corrected":
            return sig.lin_corrected
        return sig.blank_corrected  # default: Blank Corrected

    def _draw_blank_fit(self):
        """Blank fit + 95% CI band + raw blank points vs inlet number, for selected isotope."""
        canvas = self._canvas_blank_fit
        fig = canvas.figure
        fig.clear()
        sigs = self._current_isotope_signals()
        ax = fig.add_subplot(111)
        if not sigs:
            ax.text(0.5, 0.5, "No data — select an isotope", ha="center", va="center",
                    transform=ax.transAxes, fontsize=9, color="gray")
            canvas.draw_idle()
            return

        pipeline_map = {e.inlet_num: e for e in self._parsed.pipeline}

        # Blank fit line
        xi, fiti, upi, lowi = [], [], [], []
        for s in sigs:
            if s.blank_fit is not None and s.blank_fit_upper is not None and s.blank_fit_lower is not None:
                xi.append(s.inlet_num)
                fiti.append(s.blank_fit)
                upi.append(s.blank_fit_upper)
                lowi.append(s.blank_fit_lower)

        if xi:
            ax.plot(xi, fiti, color="#1565C0", linewidth=1.2, label="Blank fit", zorder=3)
            ax.fill_between(xi, lowi, upi, alpha=0.20, color="#1565C0", label="95% CI")

        # Blank signal points (blanks only)
        bx, by = [], []
        for s in sigs:
            e = pipeline_map.get(s.inlet_num)
            if e and e.position_type == "blank" and s.blank_signal is not None:
                bx.append(s.inlet_num)
                by.append(s.blank_signal)
        if bx:
            ax.scatter(bx, by, color="#64B5F6", s=24, zorder=4, label="Blank measurements")

        iso = self.cmbIsotope.currentText()
        ax.set_xlabel("Inlet Number", fontsize=7)
        ax.set_ylabel("Signal", fontsize=7)
        ax.set_title(f"Blank Fit — {iso}", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.legend(fontsize=6, loc="upper right")
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        canvas.draw_idle()

    def _draw_drift(self):
        """Drift/Reproducibility correction factor + CI band vs inlet time for selected isotope."""
        canvas = self._canvas_drift
        fig = canvas.figure
        fig.clear()
        sigs = self._current_isotope_signals()
        ax = fig.add_subplot(111)
        if not sigs:
            ax.text(0.5, 0.5, "No data — select an isotope", ha="center", va="center",
                    transform=ax.transAxes, fontsize=9, color="gray")
            canvas.draw_idle()
            return

        pipeline_map = {e.inlet_num: e for e in self._parsed.pipeline}

        xi, fiti, upi, lowi = [], [], [], []
        for s in sigs:
            if s.inlet_time is not None and s.repro_fit is not None:
                xi.append(s.inlet_time)
                fiti.append(s.repro_fit)
                upi.append(s.repro_fit_upper if s.repro_fit_upper is not None else s.repro_fit)
                lowi.append(s.repro_fit_lower if s.repro_fit_lower is not None else s.repro_fit)

        if xi:
            ax.plot(xi, fiti, color="#6A1B9A", linewidth=1.2, label="Repro fit", zorder=3)
            ax.fill_between(xi, lowi, upi, alpha=0.20, color="#6A1B9A", label="95% CI")

        # Repro signal points (standards only)
        sx, sy = [], []
        for s in sigs:
            e = pipeline_map.get(s.inlet_num)
            if e and e.position_type == "standard" and s.inlet_time is not None and s.repro_signal is not None:
                sx.append(s.inlet_time)
                sy.append(s.repro_signal)
        if sx:
            ax.scatter(sx, sy, color="#CE93D8", s=24, zorder=4, label="Standards")

        iso = self.cmbIsotope.currentText()
        ax.set_xlabel("Inlet Time", fontsize=7)
        ax.set_ylabel("Repro Factor", fontsize=7)
        ax.set_title(f"Drift/Repro Correction — {iso}", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.legend(fontsize=6, loc="upper right")
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        canvas.draw_idle()

    def _draw_linearity(self):
        """Linearity correction factor vs lin_fit_x for selected isotope."""
        canvas = self._canvas_linearity
        fig = canvas.figure
        fig.clear()
        sigs = self._current_isotope_signals()
        ax = fig.add_subplot(111)
        if not sigs:
            ax.text(0.5, 0.5, "No data — select an isotope", ha="center", va="center",
                    transform=ax.transAxes, fontsize=9, color="gray")
            canvas.draw_idle()
            return

        xi, fiti, upi, lowi = [], [], [], []
        px, py = [], []  # individual points
        for s in sigs:
            if s.lin_fit_x is not None and s.lin_fit is not None:
                xi.append(s.lin_fit_x)
                fiti.append(s.lin_fit)
                upi.append(s.lin_fit_upper if s.lin_fit_upper is not None else s.lin_fit)
                lowi.append(s.lin_fit_lower if s.lin_fit_lower is not None else s.lin_fit)
            if s.lin_fit_x is not None and s.lin_signal is not None:
                px.append(s.lin_fit_x)
                py.append(s.lin_signal)

        if xi:
            # Sort by x for a proper line
            paired = sorted(zip(xi, fiti, upi, lowi))
            xi2, fiti2, upi2, lowi2 = zip(*paired) if paired else ([], [], [], [])
            ax.plot(xi2, fiti2, color="#E65100", linewidth=1.2, label="Lin fit", zorder=3)
            ax.fill_between(xi2, lowi2, upi2, alpha=0.20, color="#E65100", label="95% CI")

        if px:
            ax.scatter(px, py, color="#FFAB91", s=24, zorder=4, label="Measurements")

        # Reference line at factor=1
        if xi:
            ax.axhline(1.0, color="#555", linewidth=0.7, linestyle="--", alpha=0.5)

        iso = self.cmbIsotope.currentText()
        ax.set_xlabel("Lin Fit X (signal)", fontsize=7)
        ax.set_ylabel("Linearity Factor", fontsize=7)
        ax.set_title(f"Linearity Correction — {iso}", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.legend(fontsize=6, loc="upper right")
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        canvas.draw_idle()

    def _draw_cal_blank(self):
        """Calibration scatter: blank-corrected signal vs known ccSTP concentration."""
        self._draw_cal_scatter(
            canvas=self._canvas_cal_blank,
            y_getter=lambda s: s.blank_corrected,
            title_prefix="Blank Corr. Signal",
            y_label="Blank Corrected Signal",
            color="#1565C0",
        )

    def _draw_cal_lin(self):
        """Calibration scatter: lin-corrected signal vs known ccSTP concentration."""
        self._draw_cal_scatter(
            canvas=self._canvas_cal_lin,
            y_getter=lambda s: s.lin_corrected,
            title_prefix="Lin Corr. Signal",
            y_label="Lin Corrected Signal",
            color="#2E7D32",
        )

    def _draw_cal_scatter(self, canvas, y_getter, title_prefix, y_label, color):
        """Common calibration scatter plot: signal vs known ccSTP (cal_x)."""
        fig = canvas.figure
        fig.clear()
        sigs = self._current_isotope_signals()
        ax = fig.add_subplot(111)
        if not sigs:
            ax.text(0.5, 0.5, "No data — select an isotope", ha="center", va="center",
                    transform=ax.transAxes, fontsize=9, color="gray")
            canvas.draw_idle()
            return

        pipeline_map = {e.inlet_num: e for e in self._parsed.pipeline}
        xs, ys, cs = [], [], []
        for s in sigs:
            if s.cal_x is None:
                continue
            y = y_getter(s)
            if y is None:
                continue
            e = pipeline_map.get(s.inlet_num)
            ptype = e.position_type if e else "invalid"
            xs.append(s.cal_x)
            ys.append(y)
            cs.append(_CHART_COLS.get(ptype, "#90A4AE"))

        if xs:
            ax.scatter(xs, ys, c=cs, s=28, zorder=3)
            # Fit line through origin (y = sensitivity * x)
            import numpy as np
            valid = [(x, y) for x, y in zip(xs, ys) if x > 0 and y > 0]
            if len(valid) >= 2:
                xv = np.array([v[0] for v in valid])
                yv = np.array([v[1] for v in valid])
                # least-squares slope through origin
                slope = float(np.dot(xv, yv) / np.dot(xv, xv))
                x_line = np.linspace(0, max(xv) * 1.05, 100)
                ax.plot(x_line, slope * x_line, color=color, linewidth=1.0,
                        linestyle="--", alpha=0.7, label=f"fit (slope={slope:.3e})")
                ax.legend(fontsize=6)
        else:
            ax.text(0.5, 0.5, "No calibration data\n(cal_x column empty)",
                    ha="center", va="center", transform=ax.transAxes, fontsize=9, color="gray")

        iso = self.cmbIsotope.currentText()
        ax.set_xlabel("Known concentration (ccSTP)", fontsize=7)
        ax.set_ylabel(y_label, fontsize=7)
        ax.set_title(f"{title_prefix} vs ccSTP — {iso}", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        canvas.draw_idle()

    def _draw_signal_progression(self):
        """
        Signal Progression diagnostic chart.

        Three stacked subplots for the selected isotope, each showing a
        different correction stage vs inlet position (numbered on X-axis):
          • Top    : Blank-corrected signal
          • Middle : Repro-corrected signal
          • Bottom : Lin-corrected signal

        Points are coloured by position type (blank / standard / air_ref / sample).
        Blanks and standards are shown with a distinct marker so they stand out.
        A thin connecting line traces the sample sequence to make drift visible.
        """
        import numpy as np
        canvas = self._canvas_signal_ts
        fig = canvas.figure
        fig.clear()

        sigs = self._current_isotope_signals()
        ax_blank, ax_repro, ax_lin = fig.subplots(3, 1, sharex=True)

        iso = self.cmbIsotope.currentText()

        if not sigs:
            for ax in (ax_blank, ax_repro, ax_lin):
                ax.text(0.5, 0.5, "No data — select an isotope",
                        ha="center", va="center", transform=ax.transAxes,
                        fontsize=9, color="gray")
            canvas.draw_idle()
            return

        pipeline_map = {e.inlet_num: e for e in self._parsed.pipeline}

        stages = [
            (ax_blank, "Blank-corrected",  lambda s: s.blank_corrected,  "#1565C0"),
            (ax_repro, "Repro-corrected",  lambda s: s.repro_corrected,  "#2E7D32"),
            (ax_lin,   "Lin-corrected",    lambda s: s.lin_corrected,    "#6A1B9A"),
        ]

        # Build sorted inlet list
        sigs_sorted = sorted(sigs, key=lambda s: s.inlet_num)
        inlet_nums  = [s.inlet_num for s in sigs_sorted]

        for ax, stage_label, getter, line_col in stages:
            # Split into samples (connected line) and references (markers only)
            samp_x, samp_y = [], []
            ref_x,  ref_y  = [], []
            ref_cols = []

            for s in sigs_sorted:
                v = getter(s)
                if v is None:
                    continue
                e    = pipeline_map.get(s.inlet_num)
                ptype = e.position_type if e else "invalid"
                col   = _CHART_COLS.get(ptype, "#90A4AE")
                if ptype == "sample":
                    samp_x.append(s.inlet_num)
                    samp_y.append(v)
                else:
                    ref_x.append(s.inlet_num)
                    ref_y.append(v)
                    ref_cols.append(col)

            # Thin connecting line through samples to reveal drift
            if samp_x:
                ax.plot(samp_x, samp_y, color=line_col, linewidth=0.8,
                        alpha=0.5, zorder=2)
                ax.scatter(samp_x, samp_y, c=_CHART_COLS["sample"],
                           s=22, zorder=3, label="sample")

            # Reference points (blanks, standards, air refs) — larger diamond marker
            if ref_x:
                ax.scatter(ref_x, ref_y, c=ref_cols, s=40,
                           marker="D", zorder=4, edgecolors="white",
                           linewidths=0.4)

            ax.set_ylabel(stage_label, fontsize=7)
            ax.tick_params(labelsize=7)
            ax.grid(True, alpha=0.2)
            ax.axhline(0, color="#999", linewidth=0.5, linestyle="--")

        ax_lin.set_xlabel("Inlet Position", fontsize=7)
        ax_blank.set_title(f"Signal Progression — {iso}", fontsize=8, fontweight="bold")

        # Shared legend (position-type colour patches)
        from matplotlib.patches import Patch
        legend_handles = [
            Patch(facecolor=_CHART_COLS[t], label=t)
            for t in ("blank", "standard", "air_ref", "sample")
            if any((pipeline_map.get(s.inlet_num).position_type if pipeline_map.get(s.inlet_num) else "invalid") == t
                   for s in sigs_sorted)
        ]
        if legend_handles:
            ax_blank.legend(handles=legend_handles, fontsize=6,
                            loc="upper right", ncol=len(legend_handles))

        fig.tight_layout()
        canvas.draw_idle()

    # ── QA/QC HTML ────────────────────────────────────────────────────────────

    def _build_qaqc_html(self) -> str:
        if not self._parsed:
            return "<p>No data parsed.</p>"

        p  = self._parsed
        r  = self._result

        lines = [
            "<html><body style='font-family:monospace; font-size:11px;'>",
            f"<h3>Noble Gas Sequence QA/QC — Run #{self._run_id}</h3>",
            f"<p><b>Folder:</b> {p.folder_path}</p>",
            f"<p><b>Main file:</b> {p.main_file}</p>",
            f"<p><b>Run hint:</b> {p.run_id_hint or '—'}</p>",
            "<hr>",
            "<table border='0' cellpadding='3'>",
            f"<tr><td>Inlets parsed:</td><td><b>{p.n_inlets}</b></td></tr>",
            f"<tr><td>Blanks:</td><td><b>{p.n_blanks}</b></td></tr>",
            f"<tr><td>Standards:</td><td><b>{p.n_standards}</b></td></tr>",
            f"<tr><td>Air references:</td><td><b>"
            f"{sum(1 for e in p.pipeline if e.position_type == 'air_ref')}"
            f"</b></td></tr>",
            f"<tr><td>Samples:</td><td><b>{p.n_samples}</b></td></tr>",
            f"<tr><td>Isotope files parsed:</td><td><b>{len(p.isotope_signals)}</b></td></tr>",
            f"<tr><td>Final Results rows:</td><td><b>{len(p.final_results)}</b></td></tr>",
        ]

        if r:
            lines += [
                f"<tr><td>DB preparations matched:</td>"
                f"<td><b>{len([ir for ir in r.inlet_results if ir.preparation_id])}</b></td></tr>",
                f"<tr><td>Unmatched final results:</td>"
                f"<td><b>{len(r.unmatched_final_results)}</b></td></tr>",
            ]

        lines.append("</table><hr>")

        # Isotope coverage
        lines.append("<h4>Isotope File Coverage</h4><ul>")
        for iso in sorted(p.isotope_signals.keys()):
            n = len(p.isotope_signals[iso])
            lines.append(f"<li>{iso}: {n} rows</li>")
        lines.append("</ul>")

        if r and r.unmatched_final_results:
            lines.append("<h4 style='color:orange;'>Unmatched Final Results</h4><ul>")
            for fr in r.unmatched_final_results:
                lines.append(f"<li>{fr.srg}</li>")
            lines.append("</ul>")

        lines.append("</body></html>")
        return "\n".join(lines)


# ─────────────────────────────── standalone test ──────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG)
    app = QApplication(sys.argv)
    dlg = NGAMNGImportDialog(run_id=1)
    dlg.show()
    sys.exit(app.exec_())
