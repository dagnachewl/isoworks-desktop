"""
ngam_ms_import_gui.py
=====================
Interactive Helix SFT MS data import dialog.

Step workflow
-------------
  1. Browse  → select file
  2. Parse   → read CSV, populate Parsed Data tab, draw cycle chart
  3. Process → fetch load list, run process_run(), populate Results + QA/QC,
               draw Overview and Sensitivity charts
  4. Save    → write ngam.ng3hesequenceresults, update ng3hesequencerun
"""
from __future__ import annotations

import html as _html
import logging
import math
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QComboBox, QDoubleSpinBox,
    QSplitter, QFileDialog, QFrame, QMessageBox,
    QSizePolicy, QTabWidget, QWidget, QTextEdit, QTextBrowser,
)
from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QColor, QFont

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings
    _HAS_WEBENGINE = True
except Exception:
    _HAS_WEBENGINE = False

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from db_core import db_manager
from sqlalchemy import text
from shared_utils import get_current_user_id, normalize_login_name
from gui_utils import show_message

from ngam_ms_parser import parse_helix_sft, HelixSFTRun
from ngam_ms_processor import (
    process_run, ProcessingConfig, RunProcessingResult,
    PositionResult, LoadListRecord, _compute_outlier_flags,
)
from ngam_processing_bridge import helix_to_protocol_sequence
from ngam_protocol_processor import (
    process_sequence,
    ProcessingConfig as ProtocolProcessingConfig,
)
from ngam_results_widget import NGAMResultsWidget

log = logging.getLogger(__name__)

# ─────────────────────────────── palette ──────────────────────────────────────
_ACCENT      = "#3D6A9E"
_HDR_SS      = (
    "QHeaderView::section {"
    "  background-color:#3D6A9E; color:white; font-weight:bold;"
    "  padding:3px 6px; border:none; border-right:1px solid #2E527A; }"
    "QHeaderView::section:last-section { border-right:none; }"
)
_TABLE_SS = (
    "QTableWidget { border:1px solid #C5CDD9; background:white; }"
    "QTableWidget::item { padding:3px 5px; }"
    "QTableWidget::item:selected { background:#DDEEFF; color:#000; }"
)
_GBX_SS = (
    "QGroupBox { background:#F5F7FA; border:1px solid #C5CDD9; border-radius:4px;"
    "  margin-top:8px; }"
    "QGroupBox::title { subcontrol-origin:margin; left:8px; padding:0 4px; }"
)
_BTN_STEP  = "QPushButton {{ background:{bg}; color:white; font-weight:bold; border:none; padding:6px 8px; border-radius:4px; text-align:left; }}" \
             "QPushButton:hover {{ background:{hov}; }}" \
             "QPushButton:disabled {{ background:#B0BEC5; color:#78909C; }}"
_BTN_GRAY  = "QPushButton { background:#7F8C8D; color:white; font-weight:bold; border:none; padding:4px 12px; border-radius:4px; } QPushButton:hover { background:#636E72; }"
_BTN_BLUE  = "QPushButton { background:#3A6EA8; color:white; font-weight:bold; border:none; padding:4px 12px; border-radius:4px; } QPushButton:hover { background:#2C5282; } QPushButton:disabled { background:#B0BEC5; color:#78909C; }"

_COL_BLANK    = QColor("#BBDEFB")
_COL_STANDARD = QColor("#C8E6C9")
_COL_AIR      = QColor("#FFF3E0")
_COL_SAMPLE   = QColor("#FFFFFF")
_COL_REJECTED = QColor("#FFCDD2")

# chart colours for position types
_CHART_COLS = {
    "blank":    "#64B5F6",
    "standard": "#81C784",
    "air_ref":  "#FFB74D",
    "sample":   "#90A4AE",
    "invalid":  "#EF9A9A",
}


# ─────────────────────────────── small helpers ────────────────────────────────

def _fmt(val: Optional[float], sig: int = 5) -> str:
    return "" if val is None else f"{val:.{sig}g}"


def _row_color(ptype: str, rejected: bool) -> QColor:
    if rejected:
        return _COL_REJECTED
    return {"blank": _COL_BLANK, "standard": _COL_STANDARD,
            "air_ref": _COL_AIR}.get(ptype, _COL_SAMPLE)


def _make_item(txt: str, color: Optional[QColor] = None,
               bold: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(txt)
    item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
    if color:
        item.setBackground(color)
    if bold:
        f = item.font(); f.setBold(True); item.setFont(f)
    return item


def _pev():
    from PyQt5.QtWidgets import QApplication
    QApplication.processEvents()


# ─────────────────────────────── help dialog ─────────────────────────────────

_HELP_DOC = Path(__file__).parent / "docs" / "ngam_ms_data_reduction.md"


def _protect_math(text: str) -> tuple:
    """Replace $$...$$ and $...$ blocks with opaque tokens before markdown
    conversion, then return the map so they can be restored afterwards.

    This prevents the markdown processor from mangling LaTeX content
    (escaping & and >, converting \\\\ line-breaks to \\, etc.).
    MathJax in the browser receives and renders the original LaTeX.
    """
    tokens: dict = {}
    counter = [0]

    def _store(delimited: str) -> str:
        key = f"XMATHX{counter[0]}X"
        counter[0] += 1
        tokens[key] = delimited
        return key

    # Display math first so $$ is never caught by the single-$ pattern
    text = re.sub(
        r"\$\$(.+?)\$\$",
        lambda m: _store(f"$${m.group(1)}$$"),
        text,
        flags=re.DOTALL,
    )
    # Inline math: $...$ not preceded/followed by another $
    text = re.sub(
        r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)",
        lambda m: _store(f"${m.group(1)}$"),
        text,
    )
    return text, tokens

_HELP_CSS_BODY = """
body  { font-family: -apple-system, Arial, sans-serif; font-size: 13px;
        color: #1a1a1a; max-width: 860px; margin: 0 auto; padding: 10px 20px; }
h1    { color: #1F3A5F; border-bottom: 2px solid #1F3A5F; padding-bottom: 4px; }
h2    { color: #2C5282; border-bottom: 1px solid #AAB8CC; padding-bottom: 2px; }
h3    { color: #2C5282; }
code  { background: #F0F4F8; padding: 1px 4px; border-radius: 3px;
        font-family: Menlo, Consolas, monospace; font-size: 12px; }
pre   { background: #F0F4F8; border-left: 3px solid #3A6EA8; padding: 10px 14px;
        border-radius: 4px; overflow-x: auto; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; }
th    { background: #1F3A5F; color: white; padding: 6px 10px; text-align: left; }
td    { border: 1px solid #C5CDD9; padding: 5px 10px; }
tr:nth-child(even) td { background: #F5F7FA; }
img   { max-width: 100%; border: 1px solid #C5CDD9; border-radius: 4px;
        margin: 8px 0; }
blockquote { border-left: 3px solid #AAB8CC; margin: 8px 0; padding: 4px 12px;
             color: #555; background: #F5F7FA; }
hr    { border: none; border-top: 1px solid #C5CDD9; margin: 12px 0; }
"""

# Full HTML document with MathJax 3 for QWebEngineView
_HELP_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<script>
MathJax = {{
  tex: {{
    inlineMath:   [['$',  '$' ]],
    displayMath:  [['$$', '$$']],
    processEscapes: true
  }},
  options: {{
    skipHtmlTags: ['script','noscript','style','textarea','pre','code']
  }}
}};
</script>
<script async
  src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js">
</script>
<style>{css}</style>
</head>
<body>{body}</body>
</html>
"""

# Lightweight fallback for QTextBrowser (no JS, no math rendering)
_HELP_CSS_FRAGMENT = f"<style>{_HELP_CSS_BODY}</style>"


class NGAMHelpDialog(QDialog):
    """Renders docs/ngam_ms_data_reduction.md in a scrollable browser window.

    Images placed in docs/img/ are resolved automatically via QTextBrowser's
    search path — just use  ![caption](img/screenshot.png)  in the markdown.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MS Data Reduction — Help & Documentation")
        self.resize(920, 740)
        self.setMinimumSize(600, 480)
        self._tmp_html = None
        self._build_ui()
        self._load_doc()

    def closeEvent(self, event):
        if self._tmp_html:
            try:
                os.remove(self._tmp_html)
            except OSError:
                pass
        super().closeEvent(event)

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # title strip
        strip = QLabel("  3He Ingrowth MS — Data Reduction Guide")
        strip.setFixedHeight(30)
        strip.setStyleSheet(
            "background:#1F3A5F; color:white; font-weight:bold; font-size:13px;"
        )
        lay.addWidget(strip)

        if _HAS_WEBENGINE:
            self._view = QWebEngineView()
            # Allow local HTML to load MathJax from the CDN
            self._view.settings().setAttribute(
                QWebEngineSettings.LocalContentCanAccessRemoteUrls, True
            )
        else:
            self._view = QTextBrowser()
            self._view.setOpenExternalLinks(True)
            self._view.setSearchPaths([str(_HELP_DOC.parent)])
            self._view.setStyleSheet("background:white; border:none;")

        lay.addWidget(self._view, 1)

        # footer
        ftr = QFrame()
        ftr.setStyleSheet("background:#EAF0F6; border-top:1px solid #AAB8CC;")
        ftr_lay = QHBoxLayout(ftr)
        ftr_lay.setContentsMargins(10, 6, 10, 6)
        lbl = QLabel(f"Source: {_HELP_DOC}")
        lbl.setStyleSheet("color:#777; font-size:10px;")
        ftr_lay.addWidget(lbl, 1)
        btn = QPushButton("Close")
        btn.setFixedHeight(28)
        btn.setStyleSheet(
            "QPushButton { background:#7F8C8D; color:white; font-weight:bold;"
            " border:none; padding:3px 16px; border-radius:4px; }"
            "QPushButton:hover { background:#636E72; }"
        )
        btn.clicked.connect(self.accept)
        ftr_lay.addWidget(btn)
        lay.addWidget(ftr)

    def _load_doc(self):
        if not _HELP_DOC.exists():
            msg = ("<p style='color:red'>Documentation file not found:<br>"
                   f"<code>{_HELP_DOC}</code></p>")
            self._view.setHtml(msg)
            return
        try:
            import markdown as _md
            raw = _HELP_DOC.read_text(encoding="utf-8")
            # Protect math blocks from markdown mangling BEFORE conversion,
            # then restore the original LaTeX (HTML-escaped so & and > are
            # safe in HTML; MathJax decodes entities before parsing LaTeX).
            protected, math_tokens = _protect_math(raw)
            body = _md.markdown(
                protected,
                extensions=["fenced_code", "tables", "toc", "attr_list"],
            )
            for token, latex in math_tokens.items():
                body = body.replace(token, _html.escape(latex))
            if _HAS_WEBENGINE:
                page = _HELP_HTML_TEMPLATE.format(css=_HELP_CSS_BODY, body=body)
                # Write to a temp file so the browser treats it as a real
                # file:// URL — this lets MathJax load from the CDN without
                # hitting Qt's local-content security restrictions.
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".html", encoding="utf-8",
                    dir=str(_HELP_DOC.parent), delete=False,
                )
                tmp.write(page)
                tmp.close()
                self._view.setUrl(QUrl.fromLocalFile(tmp.name))
                self._tmp_html = tmp.name   # keep ref; clean up on close
            else:
                self._view.setHtml(_HELP_CSS_FRAGMENT + body)
        except ImportError:
            self._view.setPlainText(_HELP_DOC.read_text(encoding="utf-8"))


# ─────────────────────────────── dialog ───────────────────────────────────────

class NGAMImportDialog(QDialog):
    """Interactive import dialog for Helix SFT MS data."""

    def __init__(self, run_id: int, datapath: str = "", parent=None):
        super().__init__(parent)
        self._run_id   = run_id
        self._helix_run: Optional[HelixSFTRun]          = None
        self._result:    Optional[RunProcessingResult]   = None
        self._load_list: List[LoadListRecord]            = []
        self._cur_pos   = 0          # 0-based index into helix_run.positions

        self.setWindowTitle(f"Import MS Data — Run #{run_id}")
        self.resize(1340, 820)
        self.setMinimumSize(1000, 640)

        self._build_ui()

        if datapath:
            self.txtFile.setText(datapath)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 4)
        root.setSpacing(4)

        # Main horizontal splitter: left controls | right charts+data
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(5)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([270, 1070])
        splitter.setStretchFactor(1, 1)

        root.addWidget(splitter, 1)
        root.addWidget(self._build_status_bar())

    # ── left panel ────────────────────────────────────────────────────────────

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)

        lay.addWidget(self._build_file_group())
        lay.addWidget(self._build_options_group())
        lay.addWidget(self._build_steps_group())
        lay.addWidget(self._build_summary_group())
        lay.addStretch()
        return w

    def _build_file_group(self) -> QGroupBox:
        grp = QGroupBox("Data File")
        grp.setStyleSheet(_GBX_SS)
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(6, 12, 6, 6)
        lay.setSpacing(4)

        self.txtFile = QLineEdit()
        self.txtFile.setReadOnly(True)
        self.txtFile.setPlaceholderText("Browse to a Helix SFT CSV…")
        self.txtFile.setStyleSheet("background:white;")

        btn_browse = QPushButton("Browse…")
        btn_browse.setStyleSheet(_BTN_GRAY)
        btn_browse.setFixedHeight(26)
        btn_browse.clicked.connect(self._browse_file)

        lay.addWidget(self.txtFile)
        lay.addWidget(btn_browse)

        lbl_run = QLabel(f"Run:  #{self._run_id}")
        f = lbl_run.font(); f.setBold(True); f.setPointSize(10); lbl_run.setFont(f)
        lbl_run.setStyleSheet(f"color:{_ACCENT};")
        lay.addWidget(lbl_run)
        return grp

    def _build_options_group(self) -> QGroupBox:
        grp = QGroupBox("Processing Options")
        grp.setStyleSheet(_GBX_SS)
        lay = QGridLayout(grp)
        lay.setContentsMargins(8, 14, 8, 8)
        lay.setHorizontalSpacing(6)
        lay.setVerticalSpacing(6)

        lay.addWidget(QLabel("Outlier method:"), 0, 0)
        self.cmbOutlier = QComboBox()
        self.cmbOutlier.addItems(["None", "N-sigma"])
        self.cmbOutlier.setCurrentIndex(1)
        lay.addWidget(self.cmbOutlier, 0, 1)

        lay.addWidget(QLabel("σ threshold:"), 1, 0)
        self.spinSigma = QDoubleSpinBox()
        self.spinSigma.setRange(1.0, 10.0)
        self.spinSigma.setSingleStep(0.5)
        self.spinSigma.setValue(2.0)
        self.spinSigma.setDecimals(1)
        lay.addWidget(self.spinSigma, 1, 1)

        # Re-process when params change (if already processed)
        self.cmbOutlier.currentIndexChanged.connect(self._on_param_changed)
        self.spinSigma.valueChanged.connect(self._on_param_changed)
        return grp

    def _build_steps_group(self) -> QGroupBox:
        grp = QGroupBox("Steps")
        grp.setStyleSheet(_GBX_SS)
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(8, 14, 8, 8)
        lay.setSpacing(6)

        def _step_btn(label, bg, hov):
            b = QPushButton(label)
            ss = _BTN_STEP.format(bg=bg, hov=hov)
            b.setStyleSheet(ss)
            b.setFixedHeight(32)
            return b

        self.btnParse   = _step_btn("1.  Parse CSV",    "#546E7A", "#37474F")
        self.btnProcess = _step_btn("2.  Process",      "#7B68EE", "#6A5ACD")
        self.btnSave    = _step_btn("3.  Save to DB",   "#27AE60", "#1E8449")
        btn_close       = _step_btn("      Close",      "#7F8C8D", "#636E72")
        btn_help        = _step_btn("?  Help / Docs",   "#3A6EA8", "#2C5282")

        self.btnProcess.setEnabled(False)
        self.btnSave.setEnabled(False)

        self.btnParse.clicked.connect(self._do_parse)
        self.btnProcess.clicked.connect(self._do_process)
        self.btnSave.clicked.connect(self._do_save)
        btn_close.clicked.connect(self.reject)
        btn_help.clicked.connect(self._show_help)

        lay.addWidget(self.btnParse)
        lay.addWidget(self.btnProcess)
        lay.addWidget(self.btnSave)
        lay.addSpacing(4)
        lay.addWidget(btn_close)
        lay.addSpacing(8)
        lay.addWidget(btn_help)
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

        self.lblSumPositions  = _val()
        self.lblSumBlanks     = _val()
        self.lblSumBackground = _val()
        self.lblSumStandards  = _val()
        self.lblSumSensitivity= _val()
        self.lblSumSamples    = _val()

        rows = [
            ("Positions:",   self.lblSumPositions),
            ("Blanks:",      self.lblSumBlanks),
            ("BG (A):",      self.lblSumBackground),
            ("Standards:",   self.lblSumStandards),
            ("Sensitivity:", self.lblSumSensitivity),
            ("Samples:",     self.lblSumSamples),
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
        vsplit.setSizes([340, 400])
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

        self._canvas_cycles      = FigureCanvas(Figure(figsize=(5, 2.8), tight_layout=True))
        self._canvas_overview    = FigureCanvas(Figure(figsize=(5, 2.8), tight_layout=True))
        self._canvas_sensitivity = FigureCanvas(Figure(figsize=(5, 2.8), tight_layout=True))

        for canvas, title in [
            (self._canvas_cycles,      "³He Cycles"),
            (self._canvas_overview,    "Run Overview"),
            (self._canvas_sensitivity, "Sensitivity Fit"),
        ]:
            canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.tabsCharts.addTab(canvas, title)

        lay.addWidget(self.tabsCharts, 1)

        # Position navigation row (for Cycles chart)
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
        self.btnPrev.clicked.connect(self._prev_pos)
        self.btnNext.clicked.connect(self._next_pos)

        self.lblPosNav = QLabel("—")
        self.lblPosNav.setAlignment(Qt.AlignCenter)
        self.lblPosNav.setStyleSheet("font-weight:bold; color:#333; font-size:10px;")

        nav.addWidget(self.btnPrev)
        nav.addStretch()
        nav.addWidget(self.lblPosNav)
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

        # Tab 1: Parsed Data
        self.tblParsed = QTableWidget(0, 8)
        self.tblParsed.setHorizontalHeaderLabels(
            ["Inlet", "Description", "Type", "Amount", "4He (V)", "3He (A)", "Cycles", "Start Time"])
        self.tblParsed.setStyleSheet(_TABLE_SS + _HDR_SS)
        self.tblParsed.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblParsed.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblParsed.verticalHeader().setVisible(False)
        self.tblParsed.currentCellChanged.connect(self._on_parsed_row_changed)
        self.tabsData.addTab(self.tblParsed, "Parsed Data")

        # Tab 2: Results
        self.tblResults = QTableWidget(0, 12)
        self.tblResults.setHorizontalHeaderLabels(
            ["Inlet", "Description", "Type", "Net 3He (A)", "± Unc",
             "Sensitivity", "Activity", "± Unc",
             "Activity Corr (TU)", "± Unc", "Corr Factor", "Notes"])
        self.tblResults.setStyleSheet(_TABLE_SS + _HDR_SS)
        self.tblResults.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblResults.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblResults.verticalHeader().setVisible(False)
        self.tabsData.addTab(self.tblResults, "Results")

        # Tab 3: QA/QC
        self.txtQAQC = QTextEdit()
        self.txtQAQC.setReadOnly(True)
        self.txtQAQC.setStyleSheet("background:white; font-size:11px;")
        self.tabsData.addTab(self.txtQAQC, "QA/QC Report")

        # Tab 4: Interactive Results (shared widget via Helix SFT bridge)
        self._interactive_results = NGAMResultsWidget(self)
        self._interactive_results.result_changed.connect(self._on_interactive_result_changed)
        self.tabsData.addTab(self._interactive_results, "Interactive")

        return self.tabsData

    # ── status bar ─────────────────────────────────────────────────────────────

    def _build_status_bar(self) -> QFrame:
        bar = QFrame()
        bar.setStyleSheet("background:#EAF0F6; border-top:1px solid #AAB8CC;")
        bar.setFixedHeight(28)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 3, 8, 3)
        self.lblStatus = QLabel("Ready.  Browse to a Helix SFT CSV, then click Parse.")
        self.lblStatus.setStyleSheet("color:#555; font-size:10px;")
        lay.addWidget(self.lblStatus)
        return bar

    # ── helpers ────────────────────────────────────────────────────────────────

    def _get_config(self) -> ProcessingConfig:
        method = {"None": "none", "N-sigma": "nsigma"}.get(
            self.cmbOutlier.currentText(), "nsigma")
        return ProcessingConfig(
            outlier_method=method,
            nsigma_threshold=self.spinSigma.value(),
        )

    def _set_status(self, msg: str, color: str = "#333"):
        self.lblStatus.setText(msg)
        self.lblStatus.setStyleSheet(f"color:{color}; font-size:10px;")
        _pev()

    def _update_nav(self):
        if self._helix_run is None:
            self.lblPosNav.setText("—")
            return
        n = len(self._helix_run.positions)
        pos = self._helix_run.positions[self._cur_pos]
        ptype = ""
        if self._result:
            for r in self._result.positions:
                if r.position_num == pos.position_num:
                    ptype = f"  [{r.position_type}]"
                    break
        self.lblPosNav.setText(
            f"Pos {self._cur_pos + 1} / {n}  —  "
            f"{pos.description[:36]}{ptype}"
        )
        self.btnPrev.setEnabled(self._cur_pos > 0)
        self.btnNext.setEnabled(self._cur_pos < n - 1)

    def _update_summary_labels(self):
        if self._helix_run:
            n_total = len(self._helix_run.positions)
            n_valid = len(self._helix_run.valid_positions)
            self.lblSumPositions.setText(f"{n_valid} / {n_total}")

        if self._result:
            r = self._result
            self.lblSumBlanks.setText(str(r.n_blanks))
            self.lblSumBackground.setText(
                f"{r.bg_mean_a:.3e}" if r.bg_mean_a else "—")
            self.lblSumStandards.setText(str(r.n_standards))
            self.lblSumSensitivity.setText(
                f"{r.sensitivity:.3e}" if r.sensitivity else "—")
            self.lblSumSamples.setText(str(r.n_samples))

    # ── actions ────────────────────────────────────────────────────────────────

    def _show_help(self):
        dlg = NGAMHelpDialog(self)
        dlg.exec_()

    def _browse_file(self):
        start = os.path.dirname(self.txtFile.text()) or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Helix SFT CSV", start,
            "CSV files (*.csv);;All files (*.*)")
        if path:
            self.txtFile.setText(path)

    def _on_param_changed(self):
        """If already processed, re-draw the cycle chart with the new sigma."""
        if self._helix_run is not None:
            self._draw_cycles(self._cur_pos)

    def _do_parse(self):
        filepath = self.txtFile.text().strip()
        if not filepath:
            show_message(self, "No File", "Browse to a CSV file first.", QMessageBox.Warning)
            return
        if not os.path.isfile(filepath):
            show_message(self, "Not Found", f"File not found:\n{filepath}", QMessageBox.Warning)
            return

        self._set_status("Parsing CSV…")
        try:
            self._helix_run = parse_helix_sft(filepath)
        except Exception as exc:
            log.exception("Parse failed")
            show_message(self, "Parse Error", str(exc), QMessageBox.Critical)
            self._set_status("Parse failed.", "#C0392B")
            return

        # Reset downstream state
        self._result = None
        self._load_list = []
        self._cur_pos = 0

        self._populate_parsed_table()
        self._update_summary_labels()
        self._update_nav()
        self._draw_cycles(0)
        self._clear_chart(self._canvas_overview)
        self._clear_chart(self._canvas_sensitivity)
        self.tblResults.setRowCount(0)
        self.txtQAQC.clear()
        self.tabsData.setCurrentIndex(0)
        self.tabsCharts.setCurrentIndex(0)

        self.btnProcess.setEnabled(True)
        self.btnSave.setEnabled(False)

        n = len(self._helix_run.positions)
        n_valid = len(self._helix_run.valid_positions)
        self._set_status(
            f"Parsed {n} positions ({n_valid} valid, {self._helix_run.n_cycles} cycles).  "
            f"Set processing options and click Process.", "#1A5276")

    def _do_process(self):
        if self._helix_run is None:
            return

        self._set_status("Fetching load list from database…")
        try:
            self._load_list = self._fetch_load_list()
        except Exception as exc:
            log.exception("Load list fetch failed")
            show_message(self, "DB Error", str(exc), QMessageBox.Critical)
            self._set_status("Could not fetch load list.", "#C0392B")
            return

        # Build ingrowth info for TU conversion (sample positions only)
        ingrowth_data = self._fetch_ingrowth_data(self._load_list)

        self._set_status("Processing…")
        try:
            self._result = process_run(
                self._helix_run, self._load_list,
                config=self._get_config(),
                run_id=self._run_id,
                ingrowth_data=ingrowth_data,
            )
        except Exception as exc:
            log.exception("Processing failed")
            show_message(self, "Processing Error", str(exc), QMessageBox.Critical)
            self._set_status("Processing failed.", "#C0392B")
            return

        # Refresh all views
        self._populate_parsed_table()   # re-paint with types from result
        self._populate_results_table()
        self.txtQAQC.setHtml(self._build_qaqc_html())
        self._update_summary_labels()
        self._update_nav()
        self._draw_cycles(self._cur_pos)
        self._draw_overview()
        self._draw_sensitivity()

        # Feed the shared Interactive tab via the bridge
        try:
            cfg = self._get_config()
            proto_seq = helix_to_protocol_sequence(
                self._helix_run,
                self._load_list,
                run_description=f"Helix SFT run #{self._run_id}",
                run_id=self._run_id,
            )
            bridge_cfg = ProtocolProcessingConfig(
                outlier_method=cfg.outlier_method,
                nsigma_threshold=cfg.nsigma_threshold,
            )
            bridge_result = process_sequence(
                proto_seq,
                config=bridge_cfg,
                ingrowth_data=ingrowth_data or None,
            )
            self._interactive_results.set_data(proto_seq, bridge_result, bridge_cfg)
        except Exception as exc:
            log.warning("Interactive tab bridge failed: %s", exc)

        self.btnSave.setEnabled(True)
        r = self._result
        self._set_status(
            f"Done.  {r.n_blanks} blanks | BG {r.bg_mean_a:.3e} A | "
            f"{r.n_standards} standards | S {r.sensitivity:.3e} A/{self._unit_name(r.unitid)} | "
            f"{r.n_samples} samples.  Review results, then Save.",
            "#1B6020")

    def _on_interactive_result_changed(self, new_result) -> None:
        """Called when the user overrides outliers in the Interactive tab."""
        total = sum(
            bf.n_outliers
            for ir in new_result.inlets
            for df in ir.block_fits.values()
            for bf in df.values()
        )
        self._set_status(
            f"Interactive re-processed — {total} outlier(s) effective "
            "(override does not affect Save; re-run Process to update main results).",
            "#5D4037",
        )

    def _do_save(self):
        if self._result is None:
            return

        try:
            user = normalize_login_name(get_current_user_id())
        except Exception:
            user = "unknown"

        now      = datetime.now()
        filepath = self.txtFile.text().strip()
        r        = self._result

        try:
            with db_manager.get_connection() as conn:
                # Safety-net: create table if migration hasn't run yet.
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS ngam.ng3hesequenceresults (
                        resultid                   SERIAL PRIMARY KEY,
                        runid                      INTEGER NOT NULL,
                        headerid                   INTEGER,
                        positioninrun              INTEGER,
                        analysisid                 INTEGER,
                        position_type              VARCHAR(20),
                        n_cycles_used              INTEGER,
                        mean4he_v                  DOUBLE PRECISION,
                        se4he_v                    DOUBLE PRECISION,
                        mean3he_a                  DOUBLE PRECISION,
                        se3he_a                    DOUBLE PRECISION,
                        bg_mean_a                  DOUBLE PRECISION,
                        bg_se_a                    DOUBLE PRECISION,
                        net3he_a                   DOUBLE PRECISION,
                        net3he_unc_a               DOUBLE PRECISION,
                        sensitivity                DOUBLE PRECISION,
                        sensitivity_unc            DOUBLE PRECISION,
                        activity                   DOUBLE PRECISION,
                        activity_unc               DOUBLE PRECISION,
                        activity_corrected         DOUBLE PRECISION,
                        activity_corrected_unc     DOUBLE PRECISION,
                        unitid                     INTEGER NOT NULL DEFAULT 1,
                        ingrowth_correction_factor DOUBLE PRECISION,
                        isblank                    BOOLEAN DEFAULT FALSE,
                        isstandard                 BOOLEAN DEFAULT FALSE,
                        isrejected                 BOOLEAN DEFAULT FALSE,
                        rejection_reason           VARCHAR(255),
                        createdatestamp            TIMESTAMP,
                        createuserstamp            VARCHAR(100),
                        UNIQUE (runid, positioninrun)
                    )
                """))

                conn.execute(text(
                    "DELETE FROM ngam.ng3hesequenceresults WHERE runid = :rid"
                ), {"rid": self._run_id})

                for pos_r in r.positions:
                    conn.execute(text("""
                        INSERT INTO ngam.ng3hesequenceresults (
                            runid, headerid, positioninrun, analysisid,
                            position_type, n_cycles_used,
                            mean4he_v, se4he_v, mean3he_a, se3he_a,
                            bg_mean_a, bg_se_a,
                            net3he_a, net3he_unc_a,
                            sensitivity, sensitivity_unc,
                            activity, activity_unc,
                            activity_corrected, activity_corrected_unc,
                            unitid, ingrowth_correction_factor,
                            isblank, isstandard, isrejected, rejection_reason,
                            createdatestamp, createuserstamp
                        ) VALUES (
                            :runid, :headerid, :pos, :analysisid,
                            :ptype, :ncycles,
                            :he4v, :se4v, :he3a, :se3a,
                            :bgm, :bgs,
                            :net, :netunc,
                            :sens, :sensunc,
                            :act, :actunc,
                            :actcorr, :actcorrunc,
                            :unitid, :igfactor,
                            :isblank, :isstd, :isrej, :reason,
                            :created, :user
                        )
                        ON CONFLICT (runid, positioninrun) DO UPDATE SET
                            position_type              = EXCLUDED.position_type,
                            n_cycles_used              = EXCLUDED.n_cycles_used,
                            mean4he_v                  = EXCLUDED.mean4he_v,
                            se4he_v                    = EXCLUDED.se4he_v,
                            mean3he_a                  = EXCLUDED.mean3he_a,
                            se3he_a                    = EXCLUDED.se3he_a,
                            bg_mean_a                  = EXCLUDED.bg_mean_a,
                            bg_se_a                    = EXCLUDED.bg_se_a,
                            net3he_a                   = EXCLUDED.net3he_a,
                            net3he_unc_a               = EXCLUDED.net3he_unc_a,
                            sensitivity                = EXCLUDED.sensitivity,
                            sensitivity_unc            = EXCLUDED.sensitivity_unc,
                            activity                   = EXCLUDED.activity,
                            activity_unc               = EXCLUDED.activity_unc,
                            activity_corrected         = EXCLUDED.activity_corrected,
                            activity_corrected_unc     = EXCLUDED.activity_corrected_unc,
                            unitid                     = EXCLUDED.unitid,
                            ingrowth_correction_factor = EXCLUDED.ingrowth_correction_factor,
                            isblank                    = EXCLUDED.isblank,
                            isstandard                 = EXCLUDED.isstandard,
                            isrejected                 = EXCLUDED.isrejected,
                            rejection_reason           = EXCLUDED.rejection_reason,
                            createdatestamp            = EXCLUDED.createdatestamp,
                            createuserstamp            = EXCLUDED.createuserstamp
                    """), {
                        "runid":   self._run_id,
                        "headerid": pos_r.headerid,
                        "pos":     pos_r.position_num,
                        "analysisid": pos_r.analysisid,
                        "ptype":   pos_r.position_type,
                        "ncycles": pos_r.n_cycles_used,
                        "he4v":    pos_r.mean_he4_v,
                        "se4v":    pos_r.se_he4_v,
                        "he3a":    pos_r.mean_he3_a,
                        "se3a":    pos_r.se_he3_a,
                        "bgm":     pos_r.bg_mean_a,
                        "bgs":     pos_r.bg_se_a,
                        "net":     pos_r.net_he3_a,
                        "netunc":  pos_r.net_he3_unc,
                        "sens":    pos_r.sensitivity,
                        "sensunc": pos_r.sensitivity_unc,
                        "act":     pos_r.activity,
                        "actunc":  pos_r.activity_unc,
                        "actcorr": pos_r.activity_corrected,
                        "actcorrunc": pos_r.activity_corrected_unc,
                        "unitid":  r.unitid if r.unitid is not None else 1,  # activity_corrected is always TU
                        "igfactor": pos_r.ingrowth_correction_factor,
                        "isblank": pos_r.position_type == "blank",
                        "isstd":   pos_r.position_type == "standard",
                        "isrej":   pos_r.isrejected,
                        "reason":  pos_r.rejection_reason[:255] if pos_r.rejection_reason else None,
                        "created": now,
                        "user":    user,
                    })

                conn.execute(text("""
                    UPDATE ngam.msrun
                    SET datapath          = :path,
                        meanbackground    = :bg,
                        meanbackgroundunc = :bgunc,
                        meanstandard      = :sens,
                        meanstandardunc   = :sensunc
                    WHERE runid = :rid AND measurement_mode = 'IG'
                """), {
                    "path":    filepath,
                    "bg":      r.bg_mean_a or None,
                    "bgunc":   r.bg_se_a or None,
                    "sens":    r.sensitivity or None,
                    "sensunc": r.sensitivity_unc or None,
                    "rid":     self._run_id,
                })

                # Compute ccSTP amounts for ingrown sample positions using the
                # response-factor method: RF = net_signal / sampleamount_ccSTP
                # of each repro-ref standard, then sample_ccSTP = net / RF.
                ll_by_pos = {rec.positioninrun: rec for rec in self._load_list}
                res_by_pos = {p.position_num: p for p in r.positions}

                # Build RF from repro-ref positions (standards with sampleamount)
                rf_values: list = []
                for pr in r.positions:
                    if pr.position_type != "standard" or pr.net_he3_a is None:
                        continue
                    ll = ll_by_pos.get(pr.position_num)
                    if ll and ll.sampleamount and ll.sampleamount > 0:
                        rf_values.append(pr.net_he3_a / ll.sampleamount)

                if rf_values:
                    rf_mean = sum(rf_values) / len(rf_values)  # A / ccSTP
                    for pr in r.positions:
                        if pr.position_type != "sample" or pr.net_he3_a is None:
                            continue
                        if pr.net_he3_a <= 0:
                            continue
                        ll = ll_by_pos.get(pr.position_num)
                        if ll is None:
                            continue
                        amount_ccstp = pr.net_he3_a / rf_mean
                        conn.execute(text("""
                            UPDATE ngam.ng3hesequenceloadlist
                               SET sampleamount = :amt
                             WHERE headerid = :hid
                        """), {"amt": amount_ccstp, "hid": ll.headerid})

                conn.execute(text("""
                    UPDATE ngam.ng3hesequenceloadlist
                       SET status = 7
                     WHERE runid = :rid AND status = 6
                """), {"rid": self._run_id})

                conn.execute(text("""
                    UPDATE ngam.msrun
                       SET runstatus = 7
                     WHERE runid = :rid AND measurement_mode = 'IG'
                """), {"rid": self._run_id})

                conn.commit()

        except Exception as exc:
            log.exception("Save failed")
            show_message(self, "Save Error", str(exc), QMessageBox.Critical)
            return

        show_message(self, "Saved",
                     f"Results for Run #{self._run_id} saved successfully.",
                     QMessageBox.Information)
        self.accept()

    # ── position navigation ────────────────────────────────────────────────────

    def _prev_pos(self):
        if self._helix_run and self._cur_pos > 0:
            self._cur_pos -= 1
            self._sync_table_selection()
            self._update_nav()
            self._draw_cycles(self._cur_pos)
            self.tabsCharts.setCurrentIndex(0)

    def _next_pos(self):
        if self._helix_run and self._cur_pos < len(self._helix_run.positions) - 1:
            self._cur_pos += 1
            self._sync_table_selection()
            self._update_nav()
            self._draw_cycles(self._cur_pos)
            self.tabsCharts.setCurrentIndex(0)

    def _on_parsed_row_changed(self, row, _col, _prow, _pcol):
        if self._helix_run is None or row < 0:
            return
        self._cur_pos = row
        self._update_nav()
        self._draw_cycles(row)
        self.tabsCharts.setCurrentIndex(0)

    def _sync_table_selection(self):
        """Keep tblParsed row in sync with _cur_pos without triggering signals."""
        self.tblParsed.blockSignals(True)
        self.tblParsed.selectRow(self._cur_pos)
        self.tblParsed.blockSignals(False)

    # ── DB: load list ──────────────────────────────────────────────────────────

    def _fetch_load_list(self) -> List[LoadListRecord]:
        records: List[LoadListRecord] = []
        with db_manager.get_connection() as conn:
            rows = conn.execute(text("""
                SELECT
                    sl.headerid,
                    sl.positioninrun,
                    sl.analysisid,
                    COALESCE(sl.sampletype, 0)            AS sampletype,
                    COALESCE(sl.isrejected, FALSE)         AS isrejected,
                    sl.knownstdactivity,                   -- TU value; NULL for ccSTP-calibrated stds
                    sl.knownstdactivityunc,
                    sl.knownstdactivityunitid,             -- unit of knownstdactivity (migration 014)
                    sl.ingrowthid,
                    -- Fall back to the current template freferenceamount when sampleamount was
                    -- not stored at create-run time (e.g. template updated after run creation).
                    COALESCE(sl.sampleamount, t.freferenceamount) AS sampleamount,
                    COALESCE(sl.bisblank,          FALSE) AS bisblank,
                    COALESCE(sl.bisreproreference, FALSE) AS bisreproreference,
                    COALESCE(sl.bislinreference,   FALSE) AS bislinreference
                FROM ngam.ng3hesequenceloadlist sl
                JOIN ngam.msrun r ON r.runid = sl.runid AND r.measurement_mode = 'IG'
                LEFT JOIN public.ngseqtemplate t
                    ON t.procedureid = r.procedureid
                    AND t.iinletid   = sl.positioninrun
                WHERE sl.runid = :rid
                ORDER BY sl.positioninrun
            """), {"rid": self._run_id}).fetchall()

        for row in rows:
            sampletype  = int(row[3])  if row[3]  is not None else 0
            known_act   = float(row[5]) if row[5] is not None else None
            known_unc   = float(row[6]) if row[6] is not None else None
            known_uid   = int(row[7])  if row[7]  is not None else None
            ingrowthid  = int(row[8])  if row[8]  is not None else None
            sampleamt   = float(row[9]) if row[9] is not None else None
            bisblank    = bool(row[10])
            bisrepro    = bool(row[11])
            bislin      = bool(row[12])
            records.append(LoadListRecord(
                headerid=int(row[0]),
                positioninrun=int(row[1]),
                analysisid=int(row[2]) if row[2] is not None else None,
                sampletype=sampletype,
                bisblank=bisblank,
                bislinreference=bislin,
                bisreproreference=bisrepro,
                knownstdactivity=known_act,
                knownstdactivityunc=known_unc,
                isrejected=bool(row[4]),
                ingrowthid=ingrowthid,
                sampleamount=sampleamt,
                knownstdactivityunitid=known_uid,
            ))
        return records

    def _fetch_ingrowth_data(
        self, load_list: List[LoadListRecord]
    ) -> "Dict[int, object]":
        """
        Query ngam.ng3heingrowthdata for every sample position that has an
        ingrowthid and return a {positioninrun: IngrowthInfo} dict.

        One ingrowthid may appear at multiple positions in the run (duplicate
        measurements of the same sample), so we build iid→positions as a
        one-to-many mapping and populate all positions for each record.

        Also fetches the sample collectiondate to compute t_sampling (sampling
        → extraction storage period) for the full TU back-correction formula:
          TU_sampling = TU_extraction × e^(λ · t_se)
        """
        from collections import defaultdict
        from ngam_protocol_processor import IngrowthInfo

        # ingrowthid → list of all positions that reference it
        iid_to_positions: dict = defaultdict(list)
        for rec in load_list:
            if rec.ingrowthid is not None:
                iid_to_positions[rec.ingrowthid].append(rec.positioninrun)

        if not iid_to_positions:
            return {}

        result: dict = {}
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT
                        ingr.ingrowthid,
                        ingr.fweightwaterbulbempty,
                        ingr.fweightwaterbulbbefore,
                        ingr.fweightwaterbulbafter,
                        ingr.dtimestart,
                        ingr.dtimeend,
                        s.collectiondate
                    FROM ngam.ng3heingrowthdata ingr
                    JOIN public.analysis a ON a.analysisid = ingr.analysisid
                    JOIN public.sample  s ON s.sampleid   = a.sampleid
                                        AND s.prefix      = a.prefix
                    WHERE ingr.ingrowthid = ANY(:ids)
                """), {"ids": list(iid_to_positions.keys())}).fetchall()

            for r in rows:
                iid            = int(r[0])
                empty          = float(r[1]) if r[1] is not None else 0.0
                bef            = float(r[2]) if r[2] is not None else 0.0
                aft            = float(r[3]) if r[3] is not None else 0.0
                t0             = r[4]   # dtimestart (extraction)
                t1             = r[5]   # dtimeend   (measurement)
                collectiondate = r[6]   # sampling date (may be None)

                if t0 is None or t1 is None:
                    log.warning("IngrowthID %d: missing timestamps — TU skipped", iid)
                    continue

                t_seconds = (t1 - t0).total_seconds()
                if t_seconds <= 0:
                    log.warning("IngrowthID %d: t_ingrowth <= 0 — TU skipped", iid)
                    continue

                # t_se: storage decay correction (sampling → extraction)
                t_sampling_s = 0.0
                if collectiondate is not None and t0 > collectiondate:
                    t_sampling_s = (t0 - collectiondate).total_seconds()
                elif collectiondate is not None:
                    log.warning(
                        "IngrowthID %d: collectiondate (%s) is after dtimestart (%s) "
                        "— t_se ignored", iid, collectiondate, t0
                    )

                for pos_num in iid_to_positions[iid]:
                    result[pos_num] = IngrowthInfo(
                        seq_num=pos_num,
                        t_ingrowth_seconds=t_seconds,
                        water_mass_before_g=bef - empty,
                        water_mass_after_g=aft - empty,
                        t_sampling_seconds=t_sampling_s,
                    )

        except Exception as exc:
            log.error("_fetch_ingrowth_data failed: %s", exc)

        return result

    @staticmethod
    def _unit_name(unitid: Optional[int]) -> str:
        """Return the short name of a measurement unit, cached across calls."""
        if unitid is None:
            return "ccSTP"
        if not hasattr(NGAMImportDialog, "_unit_cache"):
            NGAMImportDialog._unit_cache = {}
        cache = NGAMImportDialog._unit_cache
        if unitid not in cache:
            try:
                with db_manager.get_connection() as conn:
                    r = conn.execute(text(
                        "SELECT shortname FROM public.measurementunit WHERE unitid = :u"
                    ), {"u": unitid}).fetchone()
                cache[unitid] = r[0] if r else f"ID {unitid}"
            except Exception:
                cache[unitid] = f"ID {unitid}"
        return cache[unitid]

    @staticmethod
    def _fetch_unit_id(shortname: str) -> Optional[int]:
        """Look up a unit ID by short name (e.g. 'ccSTP', 'TU'). Returns None on miss."""
        try:
            with db_manager.get_connection() as conn:
                r = conn.execute(text(
                    "SELECT unitid FROM public.measurementunit WHERE shortname = :n LIMIT 1"
                ), {"n": shortname}).fetchone()
            return int(r[0]) if r else None
        except Exception:
            return None

    # ── table population ───────────────────────────────────────────────────────

    def _populate_parsed_table(self):
        tbl = self.tblParsed
        tbl.blockSignals(True)
        tbl.setRowCount(0)

        if self._helix_run is None:
            tbl.blockSignals(False)
            return

        type_map = {}
        rej_map  = {}
        used_map = {}
        if self._result:
            for r in self._result.positions:
                type_map[r.position_num] = r.position_type
                rej_map[r.position_num]  = r.isrejected
                used_map[r.position_num] = r.n_cycles_used

        amt_map: dict = {}
        if self._load_list:
            for rec in self._load_list:
                if rec.sampleamount is not None:
                    amt_map[rec.positioninrun] = rec.sampleamount

        for pos in self._helix_run.positions:
            ri = tbl.rowCount()
            tbl.insertRow(ri)
            ptype   = type_map.get(pos.position_num, "invalid" if not pos.is_valid else "sample")
            rejected = rej_map.get(pos.position_num, False)
            n_used   = used_map.get(pos.position_num,
                                    sum(1 for v in pos.he3_y if not math.isnan(v)))
            color    = _row_color(ptype, rejected)
            start_s  = pos.start_time.strftime("%Y-%m-%d %H:%M") if pos.start_time else ""
            amt     = amt_map.get(pos.position_num)
            amt_s   = f"{amt:.6g}" if amt is not None else ""
            for col, val in enumerate([
                str(pos.position_num),
                pos.description,
                ptype,
                amt_s,
                _fmt(pos.mean_he4, 5),
                _fmt(pos.mean_he3, 5),
                str(n_used),
                start_s,
            ]):
                tbl.setItem(ri, col, _make_item(val, color))

        hdr = tbl.horizontalHeader()
        for i in range(tbl.columnCount() - 1):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(tbl.columnCount() - 1, QHeaderView.Stretch)
        tbl.blockSignals(False)

    def _populate_results_table(self):
        tbl = self.tblResults
        tbl.setRowCount(0)
        if self._result is None:
            return

        for r in self._result.positions:
            if r.position_type == "invalid":
                continue
            ri = tbl.rowCount()
            tbl.insertRow(ri)
            color  = _row_color(r.position_type, r.isrejected)
            notes  = r.rejection_reason if r.rejection_reason else r.position_type
            for col, val in enumerate([
                str(r.position_num),
                r.description,
                r.position_type,
                _fmt(r.net_he3_a, 5) if r.net_he3_a is not None else "",
                _fmt(r.net_he3_unc, 3) if r.net_he3_unc is not None else "",
                _fmt(r.sensitivity, 4) if r.sensitivity is not None else "",
                _fmt(r.activity, 5) if r.activity is not None else "",
                _fmt(r.activity_unc, 3) if r.activity_unc is not None else "",
                _fmt(r.activity_corrected, 5)
                    if r.activity_corrected is not None else "",
                _fmt(r.activity_corrected_unc, 3)
                    if r.activity_corrected_unc is not None else "",
                _fmt(r.ingrowth_correction_factor, 2)
                    if r.ingrowth_correction_factor is not None else "",
                notes,
            ]):
                tbl.setItem(ri, col, _make_item(val, color))

        hdr = tbl.horizontalHeader()
        for i in range(tbl.columnCount() - 1):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(tbl.columnCount() - 1, QHeaderView.Stretch)

    # ── charts ─────────────────────────────────────────────────────────────────

    def _clear_chart(self, canvas: FigureCanvas, msg: str = ""):
        fig = canvas.figure
        fig.clear()
        if msg:
            ax = fig.add_subplot(111)
            ax.text(0.5, 0.5, msg, ha="center", va="center",
                    transform=ax.transAxes, fontsize=10, color="#999")
            ax.set_axis_off()
        canvas.draw()

    def _draw_cycles(self, pos_idx: int):
        canvas = self._canvas_cycles
        if self._helix_run is None:
            self._clear_chart(canvas, "No data parsed.")
            return

        positions = self._helix_run.positions
        if pos_idx < 0 or pos_idx >= len(positions):
            self._clear_chart(canvas)
            return

        pos  = positions[pos_idx]
        he3  = pos.he3_y
        cfg  = self._get_config()
        flgs = _compute_outlier_flags(he3, cfg).flags

        valid_x = [i for i, f in enumerate(flgs) if not f and not math.isnan(he3[i])]
        valid_y = [he3[i] for i in valid_x]
        out_x   = [i for i, f in enumerate(flgs) if f and not math.isnan(he3[i])]
        out_y   = [he3[i] for i in out_x]

        fig = canvas.figure
        fig.clear()
        ax  = fig.add_subplot(111)

        ax.scatter(valid_x, valid_y, c="#2196F3", s=36, zorder=3, label="Valid")
        if out_x:
            ax.scatter(out_x, out_y, c="#F44336", s=60, marker="x",
                       linewidths=2, zorder=4, label="Outlier")

        mu = sd = thr = 0.0
        if valid_y:
            mu  = sum(valid_y) / len(valid_y)
            sd  = math.sqrt(sum((v - mu) ** 2 for v in valid_y) / max(len(valid_y) - 1, 1))
            thr = cfg.nsigma_threshold if cfg.outlier_method == "nsigma" else 0.0
            ax.axhline(mu, color="#388E3C", lw=1.5, label=f"Mean = {mu:.3e}")
            if thr:
                ax.axhline(mu + thr * sd, color="#E91E63", lw=1.0, ls="--",
                           label=f"+{thr}σ = {mu+thr*sd:.3e}")
                ax.axhline(mu - thr * sd, color="#E91E63", lw=1.0, ls="--",
                           label=f"−{thr}σ = {mu-thr*sd:.3e}")

        if self._result and self._result.bg_mean_a:
            ax.axhline(self._result.bg_mean_a, color="#607D8B",
                       lw=1.2, ls=":", label=f"BG = {self._result.bg_mean_a:.3e}")

        # Fix y-axis to the data+sigma range; exclude BG from scaling so a
        # large background doesn't compress the sample signal to a flat line.
        _scale_y = valid_y + out_y
        if valid_y and thr:
            _scale_y = _scale_y + [mu + thr * sd, mu - thr * sd]
        if _scale_y:
            _ymin, _ymax = min(_scale_y), max(_scale_y)
            _span = _ymax - _ymin
            if _span == 0:
                _span = abs(_ymin) * 0.2 if _ymin != 0 else 1e-12
            ax.set_ylim(_ymin - 0.15 * _span, _ymax + 0.15 * _span)

        ptype = ""
        if self._result:
            for r in self._result.positions:
                if r.position_num == pos.position_num:
                    ptype = r.position_type
                    break

        n_used = len(valid_x)
        n_tot  = len([v for v in he3 if not math.isnan(v)])
        ax.set_title(
            f"Pos {pos.position_num}  [{ptype}]  —  {pos.description[:42]}\n"
            f"Cycles: {n_used}/{n_tot} used",
            fontsize=8)
        ax.set_xlabel("Cycle", fontsize=8)
        ax.set_ylabel("³He (A)", fontsize=8)
        ax.legend(fontsize=7, loc="best", framealpha=0.7)
        ax.grid(True, alpha=0.25)
        ax.tick_params(labelsize=7)
        fig.tight_layout(pad=0.6)
        canvas.draw()

    def _draw_overview(self):
        canvas = self._canvas_overview
        if self._helix_run is None:
            self._clear_chart(canvas, "Parse data first.")
            return

        positions = [p for p in self._helix_run.positions if p.is_valid]
        if not positions:
            self._clear_chart(canvas, "No valid positions.")
            return

        type_map = {}
        rej_map  = {}
        if self._result:
            for r in self._result.positions:
                type_map[r.position_num] = r.position_type
                rej_map[r.position_num]  = r.isrejected

        pos_nums = [p.position_num for p in positions]
        means    = [p.mean_he3 for p in positions]
        colors   = []
        for p in positions:
            ptype = type_map.get(p.position_num, "sample")
            rej   = rej_map.get(p.position_num, False)
            colors.append("#EF5350" if rej else _CHART_COLS.get(ptype, "#90A4AE"))

        fig = self._canvas_overview.figure
        fig.clear()
        ax  = fig.add_subplot(111)

        ax.bar(pos_nums, means, color=colors, edgecolor="white", linewidth=0.4, width=0.75)

        if self._result and self._result.bg_mean_a:
            bg = self._result.bg_mean_a
            ax.axhline(bg, color="#37474F", lw=1.5, ls="--",
                       label=f"Background = {bg:.3e} A")

        # Legend patches
        from matplotlib.patches import Patch
        legend_els = [
            Patch(color=_CHART_COLS["blank"],    label="Blank"),
            Patch(color=_CHART_COLS["standard"], label="Standard"),
            Patch(color=_CHART_COLS["air_ref"],  label="Air ref"),
            Patch(color=_CHART_COLS["sample"],   label="Sample"),
            Patch(color="#EF5350",               label="Rejected"),
        ]
        ax.legend(handles=legend_els, fontsize=7, loc="upper right", framealpha=0.7)
        ax.set_xlabel("Position", fontsize=8)
        ax.set_ylabel("Mean ³He (A)", fontsize=8)
        ax.set_title("Run Overview — Mean ³He Signal by Position", fontsize=9)
        ax.grid(True, axis="y", alpha=0.25)
        ax.tick_params(labelsize=7)
        fig.tight_layout(pad=0.6)
        canvas.draw()

    def _draw_sensitivity(self):
        canvas = self._canvas_sensitivity
        if self._result is None:
            self._clear_chart(canvas, "Process data first.")
            return

        ll_by_pos  = {rec.positioninrun: rec for rec in self._load_list}
        std_results = [r for r in self._result.positions
                       if r.position_type == "standard" and not r.isrejected]

        fig = self._canvas_sensitivity.figure
        fig.clear()
        ax  = fig.add_subplot(111)

        if not std_results:
            ax.text(0.5, 0.5, "No standard positions in this run",
                    ha="center", va="center", transform=ax.transAxes,
                    fontsize=10, color="#999")
            ax.set_title("Sensitivity Fit — Standards", fontsize=9)
            ax.set_axis_off()
            canvas.draw()
            return

        known_acts = []
        net_he3s   = []
        labels     = []
        for r in std_results:
            ll = ll_by_pos.get(r.position_num)
            if ll is None or r.net_he3_a is None:
                continue
            # Use knownstdactivity (TU) if available; fall back to sampleamount (ccSTP)
            cal = ll.knownstdactivity if (ll.knownstdactivity and ll.knownstdactivity > 0) \
                  else (ll.sampleamount if (ll.sampleamount and ll.sampleamount > 0) else None)
            if cal is not None:
                known_acts.append(cal)
                net_he3s.append(r.net_he3_a)
                labels.append(f"P{r.position_num}")

        if known_acts:
            ax.scatter(known_acts, net_he3s, c="#43A047", s=60, zorder=3, label="Standards")
            for x, y, lbl in zip(known_acts, net_he3s, labels):
                ax.annotate(lbl, (x, y), fontsize=7,
                            xytext=(4, 4), textcoords="offset points", color="#555")

            if self._result.sensitivity:
                x_max  = max(known_acts) * 1.25
                x_line = [0, x_max]
                y_line = [0, self._result.sensitivity * x_max]
                ax.plot(x_line, y_line, "b-", lw=1.5,
                        label=f"Fit: S = {self._result.sensitivity:.3e} A/{self._unit_name(self._result.unitid)}")
                # Uncertainty band
                if self._result.sensitivity_unc:
                    su = self._result.sensitivity_unc
                    ax.fill_between(x_line,
                                    [0, (self._result.sensitivity - su) * x_max],
                                    [0, (self._result.sensitivity + su) * x_max],
                                    alpha=0.12, color="blue", label="±1σ band")

        ax.set_xlim(left=0)
        ax.set_ylim(bottom=0)
        ax.set_xlabel("Known ³H Activity (TU)", fontsize=8)
        ax.set_ylabel("Net ³He Signal (A)", fontsize=8)
        ax.set_title("Sensitivity Fit — Standards", fontsize=9)
        ax.legend(fontsize=7, loc="upper left", framealpha=0.7)
        ax.grid(True, alpha=0.25)
        ax.tick_params(labelsize=7)
        fig.tight_layout(pad=0.6)
        canvas.draw()

    # ── QA/QC report ───────────────────────────────────────────────────────────

    def _build_qaqc_html(self) -> str:
        if self._result is None or self._helix_run is None:
            return "<p>No data processed.</p>"

        r    = self._result
        h    = []
        blue = "#1A5276"

        h.append(f"<h3 style='color:{blue};'>MS Run #{self._run_id} — QA/QC Report</h3>")
        h.append(f"<p><b>Generated:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}<br/>")
        h.append(f"<b>File:</b> {self.txtFile.text()}</p><hr/>")

        # Parsing
        n_tot   = len(self._helix_run.positions)
        n_valid = len(self._helix_run.valid_positions)
        h.append(f"<h4 style='color:{blue};'>Parsing</h4>")
        h.append(f"<p>Inlets: {n_tot} total, <b>{n_valid}</b> valid, "
                 f"{n_tot - n_valid} invalid<br/>"
                 f"Cycles per inlet: {self._helix_run.n_cycles}<br/>"
                 f"Outlier method: {self.cmbOutlier.currentText()}"
                 + (f", σ = {self.spinSigma.value()}"
                    if self.cmbOutlier.currentText() != "None" else "")
                 + "</p>")

        # Background
        h.append(f"<h4 style='color:{blue};'>Background</h4>")
        blanks = [pr for pr in r.positions
                  if pr.position_type == "blank" and not pr.isrejected]
        if blanks:
            bp_list = ", ".join(f"#{b.position_num}" for b in blanks)
            h.append(f"<p>Blanks used: <b>{r.n_blanks}</b> ({bp_list})<br/>")
            h.append(f"Background: <b>{r.bg_mean_a:.4e} ± {r.bg_se_a:.4e} A</b></p>")
            h.append(_html_table(
                ["Inlet", "Description", "Mean ³He (A)", "SE (A)"],
                [[str(b.position_num), b.description,
                  f"{b.mean_he3_a:.4e}" if b.mean_he3_a else "—",
                  f"{b.se_he3_a:.4e}" if b.se_he3_a else "—"]
                 for b in blanks]))
        else:
            h.append("<p style='color:#C0392B;'>⚠ No blank positions identified.</p>")

        # Sensitivity
        h.append(f"<h4 style='color:{blue};'>Sensitivity</h4>")
        stds = [pr for pr in r.positions
                if pr.position_type == "standard" and not pr.isrejected]
        ll_by_pos = {rec.positioninrun: rec for rec in self._load_list}
        if stds:
            h.append(f"<p>Standards used: <b>{r.n_standards}</b><br/>")
            h.append(f"Sensitivity: <b>{r.sensitivity:.4e} ± {r.sensitivity_unc:.4e} A/{self._unit_name(r.unitid)}</b></p>")
            rows = []
            for sp in stds:
                ll = ll_by_pos.get(sp.position_num)
                # Calibration amount: knownstdactivity (TU) or sampleamount (ccSTP)
                known = None
                if ll:
                    known = ll.knownstdactivity if (ll.knownstdactivity and ll.knownstdactivity > 0) \
                            else (ll.sampleamount if (ll.sampleamount and ll.sampleamount > 0) else None)
                derived = (sp.net_he3_a / known) if (sp.net_he3_a and known) else None
                rows.append([
                    str(sp.position_num), sp.description,
                    f"{known:.3f}" if known else "—",
                    f"{sp.net_he3_a:.4e}" if sp.net_he3_a else "—",
                    f"{derived:.4e}" if derived else "—",
                ])
            h.append(_html_table(
                ["Inlet", "Description", "Known Act. (TU)", "Net ³He (A)", "Derived S (A/TU)"],
                rows))
        else:
            h.append("<p style='color:#E67E22;'>⚠ No standards found. "
                     "Activities cannot be computed.</p>")

        # Outlier summary
        h.append(f"<h4 style='color:{blue};'>Outlier Rejection</h4>")
        tot_cyc = sum(pr.n_cycles_total for pr in r.positions if pr.n_cycles_total)
        tot_rej = sum(pr.n_cycles_total - pr.n_cycles_used
                      for pr in r.positions if pr.n_cycles_total)
        pct = 100 * tot_rej / tot_cyc if tot_cyc else 0
        h.append(f"<p>Total rejected: <b>{tot_rej}</b> / {tot_cyc} cycles ({pct:.1f}%)</p>")
        rej_rows = [pr for pr in r.positions
                    if pr.n_cycles_total > pr.n_cycles_used]
        if rej_rows:
            h.append(_html_table(
                ["Inlet", "Description", "Total", "Used", "Rejected"],
                [[str(pr.position_num), pr.description,
                  str(pr.n_cycles_total), str(pr.n_cycles_used),
                  f"<span style='color:#C0392B'>{pr.n_cycles_total - pr.n_cycles_used}</span>"]
                 for pr in rej_rows]))
        else:
            h.append("<p>No cycles rejected.</p>")

        # Sample results
        h.append(f"<h4 style='color:{blue};'>Sample Results</h4>")
        samples = [pr for pr in r.positions
                   if pr.position_type == "sample" and not pr.isrejected]
        if samples:
            h.append(_html_table(
                ["Inlet", "Description", "Net ³He (A)",
                 f"Activity ({self._unit_name(r.unitid)})", "± Unc",
                 "³H Activity (TU)", "± Unc"],
                [[str(sp.position_num), sp.description,
                  f"{sp.net_he3_a:.4e}" if sp.net_he3_a is not None else "—",
                  f"{sp.activity:.4e}" if sp.activity is not None else "—",
                  f"{sp.activity_unc:.4e}" if sp.activity_unc is not None else "—",
                  f"{sp.activity_corrected:.4e}" if sp.activity_corrected is not None else "—",
                  f"{sp.activity_corrected_unc:.4e}" if sp.activity_corrected_unc is not None else "—"]
                 for sp in samples]))
        else:
            h.append("<p>No sample positions.</p>")

        # Warnings
        warnings = []
        if r.n_blanks == 0:
            warnings.append("No blank positions — background assumed zero.")
        if r.n_standards == 0:
            warnings.append("No standards — activities cannot be computed.")
        neg = [pr for pr in r.positions
               if pr.net_he3_a is not None and pr.net_he3_a <= 0
               and pr.position_type == "sample"]
        if neg:
            warnings.append(f"{len(neg)} sample(s) at or below background (activity = 0).")
        rejected_samples = [pr for pr in r.positions
                            if pr.position_type == "sample" and pr.isrejected]
        if rejected_samples:
            warnings.append(f"{len(rejected_samples)} sample(s) marked rejected in load list.")

        if warnings:
            h.append(f"<h4 style='color:#C0392B;'>Warnings</h4><ul>")
            for w in warnings:
                h.append(f"<li style='color:#C0392B;'>{w}</li>")
            h.append("</ul>")

        return "".join(h)


# ── tiny table HTML helper ─────────────────────────────────────────────────────

def _html_table(headers: list, rows: list) -> str:
    ss = ("border:1px solid #C5CDD9; border-collapse:collapse; "
          "font-size:11px; margin-bottom:6px;")
    th_ss = ("background:#3D6A9E; color:white; font-weight:bold; "
             "padding:3px 6px; border:1px solid #2E527A;")
    td_ss = "padding:2px 6px; border:1px solid #D5DCE4;"
    h = [f"<table style='{ss}'>", "<tr>"]
    for col in headers:
        h.append(f"<th style='{th_ss}'>{col}</th>")
    h.append("</tr>")
    for row in rows:
        h.append("<tr>")
        for cell in row:
            h.append(f"<td style='{td_ss}'>{cell}</td>")
        h.append("</tr>")
    h.append("</table>")
    return "".join(h)
