"""
qaqc_gui.py — TRIMS QA/QC module for IsoWorks.

Five sub-pages selectable from a single dropdown:
  • Spiked Cells      — sfrmQAQCElectrolysis (3H enrichment parameter)
  • Deuterium Recovery — sfrmQAQCDeuterium   (2H enrichment recovery)
  • LS Counter        — sfrmQAQCStandard      (decay-corrected net CPM)
  • Lab Air Moisture  — LS Counter preset std 400
  • Control Sample    — LS Counter preset std 500

Each page shows:
  Left  (75%): matplotlib time-series chart with mean ±1σ ±2σ bands,
               trend line, outlier highlights
  Right (25%): filter panel (date presets, system/cell, apply)
               + status badge (GREEN/AMBER/RED)
               + statistics panel
               + legend
  Bottom:      data grid, outlier rows highlighted in red

Header bar: Parameter dropdown | Export (CSV) | Report (HTML)
"""
from __future__ import annotations

import base64
import io
import math
import logging
import os
import tempfile
import webbrowser
from datetime import datetime, date
from typing import Optional, List, Tuple

import numpy as np

from PyQt5.QtCore import Qt, QDate
from PyQt5.QtGui import (
    QStandardItemModel, QStandardItem, QColor, QBrush, QFont
)
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QLineEdit, QComboBox, QCheckBox,
    QPushButton, QDateEdit, QTableView, QHeaderView,
    QAbstractItemView, QSizePolicy, QFormLayout, QFrame,
    QApplication, QFileDialog, QStackedWidget
)
from sqlalchemy import text
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.dates as mdates
import matplotlib.patches as mpatches

from db_core import db_manager
from gui_utils import show_message

log = logging.getLogger(__name__)

# ── colour palette ───────────────────────────────────────────────────────────
_COL_DATA     = "#1565C0"   # blue        – normal measurement points
_COL_MEAN     = "#2E7D32"   # green       – long-term mean
_COL_1S       = "#AB00D6"   # magenta     – ±1σ
_COL_2S       = "#D32F2F"   # red         – ±2σ
_COL_OUTLIER  = "#FF6F00"   # amber       – outlier marker ring
_COL_TREND    = "#78909C"   # blue-grey   – linear trend line

CELL_SPIKE = 3

# ── status thresholds (fraction of recent N points outside ±2σ) ─────────────
_STATUS_N       = 10   # look at last N points
_STATUS_RED_PCT = 0.3  # ≥30 % outside ±2σ → RED
_STATUS_AMB_PCT = 0.1  # ≥10 %             → AMBER


# ============================================================================
#  Tiny helpers
# ============================================================================
def _fmt(v, decimals=2):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _quadrature_mean_unc(values, uncs):
    """Return 1/n × √Σuᵢ²."""
    n = len([v for v in values if v is not None])
    if n == 0:
        return None
    s = sum((u ** 2) for u in uncs if u is not None)
    return (1 / n) * math.sqrt(s)


def _outlier_mask(values: list, mean: float, stdev: float) -> list:
    """Return list of bools — True where |v - mean| > 2σ."""
    if stdev == 0:
        return [False] * len(values)
    return [abs(v - mean) > 2 * stdev for v in values]


def _status_from_mask(mask: list) -> str:
    """Return 'GREEN', 'AMBER', or 'RED' based on recent outlier fraction."""
    recent = mask[-_STATUS_N:]
    if not recent:
        return "GREEN"
    frac = sum(recent) / len(recent)
    if frac >= _STATUS_RED_PCT:
        return "RED"
    if frac >= _STATUS_AMB_PCT:
        return "AMBER"
    return "GREEN"


def _trend_line(dates_dt: list, values: list) -> Tuple[list, list]:
    """Fit and return (xs, ys) for a linear trend line through the data.

    Returns ([], []) if the fit cannot be computed (too few points, all dates
    identical, or non-finite values).
    """
    if len(dates_dt) < 2:
        return [], []
    xs = np.array([(d - dates_dt[0]).total_seconds() / 86400 for d in dates_dt])
    ys = np.array(values, dtype=float)
    # Degenerate: all x-values identical (zero spread) or any non-finite value
    if np.ptp(xs) == 0 or not np.all(np.isfinite(xs)) or not np.all(np.isfinite(ys)):
        return [], []
    try:
        coeffs = np.polyfit(xs, ys, 1)
        y_fit = np.polyval(coeffs, xs)
        return dates_dt, y_fit.tolist()
    except (np.linalg.LinAlgError, ValueError):
        return [], []


# ============================================================================
#  Status badge
# ============================================================================
class _StatusBadge(QLabel):
    _STYLES = {
        "GREEN": ("● In Control",  "#1B5E20", "#E8F5E9"),
        "AMBER": ("▲ Monitor",     "#E65100", "#FFF3E0"),
        "RED":   ("✖ Out of Control", "#B71C1C", "#FFEBEE"),
        "NONE":  ("— No Data",     "#555555", "#F5F5F5"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(24)
        self.setFont(QFont("Arial", 9, QFont.Bold))
        self.set_status("NONE")

    def set_status(self, status: str):
        text, fg, bg = self._STYLES.get(status, self._STYLES["NONE"])
        self.setText(text)
        self.setStyleSheet(
            f"color:{fg};background:{bg};border-radius:4px;"
            f"padding:2px 6px;border:1px solid {fg};"
        )


# ============================================================================
#  Date preset buttons
# ============================================================================
class _DatePresets(QWidget):
    def __init__(self, dt_from: QDateEdit, dt_to: QDateEdit, parent=None):
        super().__init__(parent)
        self._from = dt_from
        self._to   = dt_to
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        for label, months in [("6M", 6), ("1Y", 12), ("2Y", 24), ("All", None)]:
            btn = QPushButton(label)
            btn.setFixedHeight(20)
            btn.setStyleSheet(
                "QPushButton{font-size:8pt;padding:0 4px;"
                "background:#ECEFF1;border:1px solid #B0BEC5;border-radius:3px;}"
                "QPushButton:hover{background:#CFD8DC;}"
            )
            btn.clicked.connect(lambda _, m=months: self._apply(m))
            lay.addWidget(btn)

    def _apply(self, months: Optional[int]):
        today = QDate.currentDate()
        self._to.setDate(today)
        if months is None:
            self._from.setDate(QDate(2000, 1, 1))
        else:
            self._from.setDate(today.addMonths(-months))


# ============================================================================
#  Shared chart widget — now with trend line, outlier markers, annotation
# ============================================================================
class _QaQcChart(QWidget):
    def __init__(self, y_label: str, parent=None):
        super().__init__(parent)
        self.y_label = y_label
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.fig = Figure(figsize=(6, 3.5), tight_layout=True, facecolor="#FAFAFA")
        self.canvas = FigureCanvas(self.fig)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lay.addWidget(self.canvas)
        self.ax = self.fig.add_subplot(111)
        self._init_axes()

    def _init_axes(self):
        self.ax.clear()
        self.ax.set_xlabel("Measurement Date", fontsize=9)
        self.ax.set_ylabel(self.y_label, fontsize=9)
        self.ax.tick_params(labelsize=8)
        self.ax.grid(True, linestyle="--", alpha=0.4)
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        self.fig.autofmt_xdate(rotation=30, ha="right")

    def plot(self, dates: list, values: list, mean: float, stdev: float,
             outlier_mask: Optional[list] = None) -> list:
        """
        Draw the control chart.  Returns list of indices where outlier_mask is True.
        """
        self.ax.clear()
        self._init_axes()

        if not dates:
            self.canvas.draw_idle()
            return []

        if outlier_mask is None:
            outlier_mask = _outlier_mask(values, mean, stdev)

        normal_dates  = [d for d, o in zip(dates, outlier_mask) if not o]
        normal_vals   = [v for v, o in zip(values, outlier_mask) if not o]
        outlier_dates = [d for d, o in zip(dates, outlier_mask) if o]
        outlier_vals  = [v for v, o in zip(values, outlier_mask) if o]

        # Trend line (all points)
        tx, ty = _trend_line(dates, values)
        if tx:
            self.ax.plot(tx, ty, color=_COL_TREND, linewidth=1.0,
                         linestyle="--", alpha=0.7, zorder=2, label="Trend")

        # Connecting line (all points, sorted)
        sorted_pairs = sorted(zip(dates, values))
        xs, ys = zip(*sorted_pairs)
        self.ax.plot(xs, ys, color=_COL_DATA, linewidth=0.6, alpha=0.4, zorder=3)

        # Normal points
        if normal_dates:
            self.ax.scatter(normal_dates, normal_vals, color=_COL_DATA,
                            s=30, zorder=5)

        # Outlier points — amber ring over data colour
        if outlier_dates:
            self.ax.scatter(outlier_dates, outlier_vals, color=_COL_DATA,
                            s=30, zorder=6)
            self.ax.scatter(outlier_dates, outlier_vals,
                            edgecolors=_COL_OUTLIER, facecolors="none",
                            s=90, linewidths=2, zorder=7)

        xmin, xmax = min(dates), max(dates)

        # ±2σ bands (shaded)
        self.ax.axhspan(mean - 2*stdev, mean + 2*stdev,
                        color=_COL_2S, alpha=0.06, zorder=1)
        self.ax.axhspan(mean - stdev, mean + stdev,
                        color=_COL_1S, alpha=0.08, zorder=1)

        # ±2σ lines
        self.ax.hlines(mean + 2*stdev, xmin, xmax, colors=_COL_2S,
                       linestyles="dashed", linewidth=1.2)
        self.ax.hlines(mean - 2*stdev, xmin, xmax, colors=_COL_2S,
                       linestyles="dashed", linewidth=1.2)
        # ±1σ lines
        self.ax.hlines(mean + stdev, xmin, xmax, colors=_COL_1S,
                       linestyles=(0, (4, 3)), linewidth=1.2)
        self.ax.hlines(mean - stdev, xmin, xmax, colors=_COL_1S,
                       linestyles=(0, (4, 3)), linewidth=1.2)
        # Mean
        self.ax.hlines(mean, xmin, xmax, colors=_COL_MEAN,
                       linestyles="solid", linewidth=1.8)

        # n= and outlier count annotation
        n_out = sum(outlier_mask)
        note = f"n={len(values)}"
        if n_out:
            note += f"  ({n_out} outside ±2σ)"
        self.ax.text(0.01, 0.98, note, transform=self.ax.transAxes,
                     fontsize=7.5, va="top", ha="left",
                     color="#555", style="italic")

        # Legend patches
        patches = [
            mpatches.Patch(color=_COL_DATA,    label="Normal"),
            mpatches.Patch(color=_COL_MEAN,    label=f"Mean ({_fmt(mean)})"),
            mpatches.Patch(color=_COL_1S,      label=f"±1σ ({_fmt(stdev)})"),
            mpatches.Patch(color=_COL_2S,      label="±2σ"),
            mpatches.Patch(color=_COL_TREND,   label="Trend"),
        ]
        if n_out:
            patches.append(
                mpatches.Patch(facecolor="none",
                               edgecolor=_COL_OUTLIER, linewidth=2,
                               label=f"Outlier (n={n_out})")
            )
        self.ax.legend(handles=patches, loc="lower right", fontsize=7.5,
                       framealpha=0.85, edgecolor="#ccc")

        self.canvas.draw_idle()
        return [i for i, o in enumerate(outlier_mask) if o]

    def clear(self):
        self.ax.clear()
        self._init_axes()
        self.canvas.draw_idle()

    def to_png_bytes(self) -> bytes:
        """Render the current figure to PNG bytes (for reports)."""
        buf = io.BytesIO()
        self.fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                         facecolor=self.fig.get_facecolor())
        return buf.getvalue()


# ============================================================================
#  Shared data table  — supports per-row background colour
# ============================================================================
class _DataTable(QTableView):
    def __init__(self, headers: list, parent=None):
        super().__init__(parent)
        self._headers = headers
        self._model = QStandardItemModel(0, len(headers))
        self._model.setHorizontalHeaderLabels(headers)
        self.setModel(self._model)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)
        self.verticalHeader().hide()
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.setMaximumHeight(200)

    def populate(self, rows: list, outlier_indices: Optional[set] = None,
                 headers: Optional[list] = None):
        if headers and headers != self._headers:
            self._headers = headers
            self._model.setColumnCount(len(headers))
            self._model.setHorizontalHeaderLabels(headers)
        self._model.setRowCount(0)
        outlier_indices = outlier_indices or set()
        _out_bg = QColor("#FFF3E0")
        _out_fg = QColor("#E65100")
        for row_idx, row_data in enumerate(rows):
            is_out = row_idx in outlier_indices
            items = []
            for v in row_data:
                it = QStandardItem(str(v) if v is not None else "")
                it.setTextAlignment(Qt.AlignCenter)
                if is_out:
                    it.setBackground(QBrush(_out_bg))
                    it.setForeground(QBrush(_out_fg))
                items.append(it)
            self._model.appendRow(items)


# ============================================================================
#  Shared filter-panel base with date presets + status badge
# ============================================================================
class _FilterPanelBase(QGroupBox):
    """Provides date presets + status badge; subclasses add domain controls."""

    def __init__(self, title="Filters", parent=None):
        super().__init__(title, parent)
        self.setFixedWidth(238)
        self._outer = QVBoxLayout(self)
        self._outer.setSpacing(5)

    def _build_date_block(self) -> Tuple[QDateEdit, QDateEdit]:
        """Add date-preset buttons + From/To pickers; return (dtFrom, dtTo)."""
        dtFrom = QDateEdit()
        dtFrom.setCalendarPopup(True)
        dtFrom.setDate(QDate.currentDate().addYears(-2))
        dtFrom.setDisplayFormat("yyyy-MM-dd")
        dtTo = QDateEdit()
        dtTo.setCalendarPopup(True)
        dtTo.setDate(QDate.currentDate())
        dtTo.setDisplayFormat("yyyy-MM-dd")

        presets = _DatePresets(dtFrom, dtTo, self)
        self._outer.addWidget(presets)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(3)
        form.addRow("From:", dtFrom)
        form.addRow("To:", dtTo)
        self._outer.addLayout(form)
        return dtFrom, dtTo

    def _build_apply_button(self) -> QPushButton:
        btn = QPushButton("Apply Filter")
        btn.setStyleSheet(
            "QPushButton{background:#1565C0;color:white;border-radius:4px;padding:4px 8px;}"
            "QPushButton:hover{background:#1976D2;}"
        )
        self._outer.addWidget(btn)
        return btn

    def _build_separator(self):
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        self._outer.addWidget(sep)

    def _build_status_badge(self) -> _StatusBadge:
        badge = _StatusBadge(self)
        self._outer.addWidget(badge)
        return badge

    def _build_legend(self):
        lbl = QLabel("Legend")
        lbl.setFont(QFont("Arial", 8, QFont.Bold))
        lbl.setStyleSheet("color:#607D8B;")
        self._outer.addWidget(lbl)
        legend_widget = QWidget()
        lay = QVBoxLayout(legend_widget)
        lay.setSpacing(2)
        lay.setContentsMargins(0, 0, 0, 0)
        items = [
            (_COL_2S,     "±2σ band"),
            (_COL_1S,     "±1σ band"),
            (_COL_MEAN,   "Mean"),
            (_COL_TREND,  "Trend line"),
            (_COL_OUTLIER,"Outlier (>2σ)"),
        ]
        for color, txt in items:
            row = QHBoxLayout()
            lne = QFrame()
            lne.setFixedSize(24, 3)
            lne.setStyleSheet(f"background:{color};border:none;")
            row.addWidget(lne)
            row.addWidget(QLabel(txt))
            row.addStretch()
            lay.addLayout(row)
        # outlier dot
        self._outer.addWidget(legend_widget)
        self._outer.addStretch()


# ============================================================================
#  Electrolysis filter panel (Spiked Cells + Deuterium)
# ============================================================================
class _ElysFilterPanel(_FilterPanelBase):
    def __init__(self, title="Filters", parent=None):
        super().__init__(title, parent)

        # All Systems
        self.chkAllSystems = QCheckBox("All Systems")
        self.chkAllSystems.setChecked(True)
        self._outer.addWidget(self.chkAllSystems)

        # System + Cell
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(3)
        self.cmbSystem = QComboBox()
        self.cmbSystem.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        form.addRow("System:", self.cmbSystem)

        cell_row = QHBoxLayout()
        self.btnPrev = QPushButton("◀"); self.btnPrev.setFixedWidth(24)
        self.cmbCell = QComboBox()
        self.cmbCell.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btnNext = QPushButton("▶"); self.btnNext.setFixedWidth(24)
        cell_row.addWidget(self.btnPrev)
        cell_row.addWidget(self.cmbCell)
        cell_row.addWidget(self.btnNext)
        form.addRow("Cell:", cell_row)
        self._outer.addLayout(form)

        # Dates
        self.dtFrom, self.dtTo = self._build_date_block()
        self.btnApply = self._build_apply_button()
        self._build_separator()
        self.badge = self._build_status_badge()
        self._build_separator()

        # Stats
        lbl_stats = QLabel("Cell Statistics")
        lbl_stats.setFont(QFont("Arial", 9, QFont.Bold))
        lbl_stats.setStyleSheet("color:#1565C0;")
        self._outer.addWidget(lbl_stats)

        sf = QFormLayout()
        sf.setLabelAlignment(Qt.AlignRight)
        sf.setSpacing(3)
        self.txtAvgEP    = QLineEdit(); self.txtAvgEP.setReadOnly(True)
        self.txtAvgEPUnc = QLineEdit(); self.txtAvgEPUnc.setReadOnly(True)
        self.txtAvgEF    = QLineEdit(); self.txtAvgEF.setReadOnly(True)
        self.txtAvgEFUnc = QLineEdit(); self.txtAvgEFUnc.setReadOnly(True)
        self.txtN        = QLineEdit(); self.txtN.setReadOnly(True)
        self.txtStdev    = QLineEdit(); self.txtStdev.setReadOnly(True)
        for w in [self.txtAvgEP, self.txtAvgEPUnc, self.txtAvgEF,
                  self.txtAvgEFUnc, self.txtN, self.txtStdev]:
            w.setFixedWidth(68); w.setAlignment(Qt.AlignRight)

        ep_row = QHBoxLayout(); ep_row.addWidget(self.txtAvgEP)
        ep_row.addWidget(QLabel("±")); ep_row.addWidget(self.txtAvgEPUnc)
        ef_row = QHBoxLayout(); ef_row.addWidget(self.txtAvgEF)
        ef_row.addWidget(QLabel("±")); ef_row.addWidget(self.txtAvgEFUnc)
        sf.addRow("Avg EP:", ep_row)
        sf.addRow("Avg EF:", ef_row)
        sf.addRow("n:", self.txtN)
        sf.addRow("σ:", self.txtStdev)
        self._outer.addLayout(sf)

        self._build_separator()
        self._build_legend()

    def clear_stats(self):
        for w in [self.txtAvgEP, self.txtAvgEPUnc, self.txtAvgEF,
                  self.txtAvgEFUnc, self.txtN, self.txtStdev]:
            w.clear()
        self.badge.set_status("NONE")

    def set_stats(self, avg_ep, avg_ep_unc, avg_ef, avg_ef_unc, n, stdev, status):
        self.txtAvgEP.setText(_fmt(avg_ep))
        self.txtAvgEPUnc.setText(_fmt(avg_ep_unc))
        self.txtAvgEF.setText(_fmt(avg_ef))
        self.txtAvgEFUnc.setText(_fmt(avg_ef_unc))
        self.txtN.setText(str(n))
        self.txtStdev.setText(_fmt(stdev))
        self.badge.set_status(status)


# ============================================================================
#  LSC counter filter panel
# ============================================================================
class _LSCFilterPanel(_FilterPanelBase):
    def __init__(self, parent=None):
        super().__init__("Filters", parent)

        self.chkAllCounters = QCheckBox("All Counters")
        self.chkAllCounters.setChecked(False)
        self._outer.addWidget(self.chkAllCounters)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(3)
        self.cmbCounter = QComboBox()
        self.cmbCounter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        form.addRow("Counter:", self.cmbCounter)
        self.cmbStandard = QComboBox()
        self.cmbStandard.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        form.addRow("Standard:", self.cmbStandard)
        self._outer.addLayout(form)

        self.dtFrom, self.dtTo = self._build_date_block()
        self.btnApply = self._build_apply_button()
        self._build_separator()
        self.badge = self._build_status_badge()
        self._build_separator()

        lbl_stats = QLabel("Counter Statistics")
        lbl_stats.setFont(QFont("Arial", 9, QFont.Bold))
        lbl_stats.setStyleSheet("color:#1565C0;")
        self._outer.addWidget(lbl_stats)

        sf = QFormLayout()
        sf.setLabelAlignment(Qt.AlignRight)
        sf.setSpacing(3)
        self.txtAvgCPM = QLineEdit(); self.txtAvgCPM.setReadOnly(True)
        self.txtAvgEff = QLineEdit(); self.txtAvgEff.setReadOnly(True)
        self.txtBkg    = QLineEdit(); self.txtBkg.setReadOnly(True)
        self.txtCalib  = QLineEdit(); self.txtCalib.setReadOnly(True)
        self.txtFOM    = QLineEdit(); self.txtFOM.setReadOnly(True)
        self.txtN      = QLineEdit(); self.txtN.setReadOnly(True)
        self.txtStdev  = QLineEdit(); self.txtStdev.setReadOnly(True)
        for w in [self.txtAvgCPM, self.txtAvgEff, self.txtBkg,
                  self.txtCalib, self.txtFOM, self.txtN, self.txtStdev]:
            w.setAlignment(Qt.AlignRight)
        sf.addRow("Avg CPM:", self.txtAvgCPM)
        sf.addRow("Efficiency %:", self.txtAvgEff)
        sf.addRow("Background:", self.txtBkg)
        sf.addRow("Calibration:", self.txtCalib)
        sf.addRow("FOM:", self.txtFOM)
        sf.addRow("n:", self.txtN)
        sf.addRow("σ:", self.txtStdev)
        self._outer.addLayout(sf)

        self._build_separator()
        self._build_legend()

    def clear_stats(self):
        for w in [self.txtAvgCPM, self.txtAvgEff, self.txtBkg,
                  self.txtCalib, self.txtFOM, self.txtN, self.txtStdev]:
            w.clear()
        self.badge.set_status("NONE")


# ============================================================================
#  HTML report generator
# ============================================================================
def _build_html_report(
    mode_title: str,
    filter_summary: str,
    stats: dict,
    headers: list,
    rows: list,
    outlier_indices: set,
    png_bytes: bytes,
) -> str:
    # Embed chart
    img_b64 = base64.b64encode(png_bytes).decode()
    img_tag = f'<img src="data:image/png;base64,{img_b64}" style="width:100%;max-width:900px;">'

    # Statistics cards
    stat_html = ""
    for k, v in stats.items():
        stat_html += f"""
        <div class="stat-card">
          <div class="stat-label">{k}</div>
          <div class="stat-value">{v}</div>
        </div>"""

    # Status badge
    status = stats.get("Status", "—")
    badge_color = {"● In Control": "#1B5E20", "▲ Monitor": "#E65100",
                   "✖ Out of Control": "#B71C1C"}.get(status, "#555")

    # Data table
    thead = "".join(f"<th>{h}</th>" for h in headers)
    tbody = ""
    for i, row in enumerate(rows):
        is_out = i in outlier_indices
        row_style = ' class="outlier"' if is_out else ""
        cells = "".join(f"<td>{v if v is not None else ''}</td>" for v in row)
        tbody += f"<tr{row_style}>{cells}</tr>\n"

    # Outlier summary
    outlier_rows_html = ""
    if outlier_indices:
        for i in sorted(outlier_indices):
            row = rows[i]
            outlier_rows_html += (
                f"<tr><td>{row[0]}</td><td>{row[1]}</td>"
                f"<td>{row[2]}</td><td>{row[3]}</td></tr>\n"
            )

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>QA/QC Report — {mode_title}</title>
<style>
  body{{font-family:'Segoe UI',Arial,sans-serif;font-size:13px;
        background:#F5F7FA;color:#212121;margin:0;padding:0;}}
  .page{{max-width:960px;margin:0 auto;padding:24px;}}
  .header{{background:#00695C;color:white;padding:16px 24px;
           border-radius:8px;margin-bottom:20px;}}
  .header h1{{margin:0;font-size:20px;font-weight:600;}}
  .header p{{margin:4px 0 0;font-size:12px;opacity:0.85;}}
  .badge{{display:inline-block;padding:3px 10px;border-radius:12px;
          font-weight:bold;font-size:12px;
          color:white;background:{badge_color};margin-top:6px;}}
  .section{{background:white;border-radius:8px;padding:16px 20px;
            margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.1);}}
  .section h2{{margin:0 0 12px;font-size:14px;color:#00695C;
               border-bottom:1px solid #E0E0E0;padding-bottom:6px;}}
  .stat-grid{{display:flex;flex-wrap:wrap;gap:10px;}}
  .stat-card{{background:#F9FBE7;border:1px solid #DCE775;border-radius:6px;
              padding:8px 14px;min-width:120px;}}
  .stat-label{{font-size:10px;color:#827717;text-transform:uppercase;}}
  .stat-value{{font-size:15px;font-weight:bold;color:#33691E;margin-top:2px;}}
  table{{width:100%;border-collapse:collapse;font-size:12px;}}
  th{{background:#E0F2F1;color:#004D40;padding:6px 8px;text-align:left;
      border-bottom:2px solid #80CBC4;}}
  td{{padding:5px 8px;border-bottom:1px solid #EEEEEE;}}
  tr:hover td{{background:#F5F5F5;}}
  tr.outlier td{{background:#FFF3E0;color:#E65100;font-weight:500;}}
  .filter-tag{{display:inline-block;background:#E0F7FA;color:#006064;
               border-radius:4px;padding:2px 8px;margin:2px;font-size:11px;}}
  .footer{{text-align:center;font-size:11px;color:#9E9E9E;margin-top:20px;}}
</style>
</head>
<body>
<div class="page">

  <div class="header">
    <h1>QA/QC Control Report — {mode_title}</h1>
    <p>Generated: {ts}</p>
    <p>Filters: {filter_summary}</p>
    <span class="badge">{status}</span>
  </div>

  <div class="section">
    <h2>Control Chart</h2>
    {img_tag}
  </div>

  <div class="section">
    <h2>Summary Statistics</h2>
    <div class="stat-grid">{stat_html}</div>
  </div>

  {'<div class="section"><h2>Outliers (' + str(len(outlier_indices)) + ' points outside ±2σ)</h2>' +
   '<table><thead><tr><th>Date</th><th>System/Counter</th><th>Cell/Run</th><th>Value</th></tr></thead>'
   '<tbody>' + outlier_rows_html + '</tbody></table></div>'
   if outlier_indices else ''}

  <div class="section">
    <h2>Data ({len(rows)} records)</h2>
    <table>
      <thead><tr>{thead}</tr></thead>
      <tbody>{tbody}</tbody>
    </table>
  </div>

  <div class="footer">
    IsoWorks pyLIMS — QA/QC Module &nbsp;|&nbsp; {ts}
  </div>
</div>
</body>
</html>"""


# ============================================================================
#  Page: Spiked Cells (3H enrichment parameter)
# ============================================================================
class _SpikedCellsPage(QWidget):
    _TABLE_HEADERS = ["Run Date", "System", "Cell", "EP", "EP Unc", "EF", "EF Unc", "Flag"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list = []
        self._outlier_indices: set = set()
        self._mean = 0.0
        self._stdev = 0.0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        top = QHBoxLayout()
        self._chart = _QaQcChart("Enrichment Parameter", self)
        top.addWidget(self._chart, stretch=1)

        self._filter = _ElysFilterPanel("Filter", self)
        self._filter.chkAllSystems.toggled.connect(self._on_all_systems)
        self._filter.cmbSystem.currentIndexChanged.connect(self._on_system_changed)
        self._filter.btnApply.clicked.connect(self.refresh)
        self._filter.btnNext.clicked.connect(self._next_cell)
        self._filter.btnPrev.clicked.connect(self._prev_cell)
        top.addWidget(self._filter)
        lay.addLayout(top, stretch=1)

        self._table = _DataTable(self._TABLE_HEADERS, self)
        lay.addWidget(self._table)

        self._load_systems()
        self.refresh()

    def _load_systems(self):
        self._filter.cmbSystem.blockSignals(True)
        self._filter.cmbSystem.clear()
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT e.EquipmentID, e.EquipmentName "
                    "FROM Equipment e "
                    "JOIN ElectrolysisSystem es ON e.EquipmentID = es.ElysSystemID "
                    "WHERE e.CategoryID = 2 ORDER BY e.EquipmentID"
                )).fetchall()
            for r in rows:
                self._filter.cmbSystem.addItem(
                    f"{r.EquipmentID} – {r.EquipmentName}", r.EquipmentID)
        except Exception as exc:
            log.warning("Spiked cells: load systems: %s", exc)
        self._filter.cmbSystem.blockSignals(False)
        if self._filter.cmbSystem.count():
            self._on_system_changed()

    def _on_all_systems(self, checked):
        self._filter.cmbSystem.setEnabled(not checked)
        self._filter.cmbCell.setEnabled(not checked)
        self._filter.btnPrev.setEnabled(not checked)
        self._filter.btnNext.setEnabled(not checked)
        self.refresh()

    def _on_system_changed(self):
        sys_id = self._filter.cmbSystem.currentData()
        self._filter.cmbCell.blockSignals(True)
        self._filter.cmbCell.clear()
        if sys_id is not None:
            try:
                with db_manager.get_connection() as conn:
                    rows = conn.execute(text(
                        "SELECT CellID FROM ElectrolysisCell "
                        "WHERE SystemID = :sid ORDER BY CellID"
                    ), {"sid": sys_id}).fetchall()
                for r in rows:
                    self._filter.cmbCell.addItem(str(r.CellID), r.CellID)
            except Exception as exc:
                log.warning("Spiked cells: load cells: %s", exc)
        self._filter.cmbCell.blockSignals(False)
        self.refresh()

    def _next_cell(self):
        cb = self._filter.cmbCell
        cb.setCurrentIndex((cb.currentIndex() + 1) % cb.count() if cb.count() else 0)
        self.refresh()

    def _prev_cell(self):
        cb = self._filter.cmbCell
        cb.setCurrentIndex((cb.currentIndex() - 1) % cb.count() if cb.count() else 0)
        self.refresh()

    def refresh(self):
        all_sys = self._filter.chkAllSystems.isChecked()
        sys_id  = None if all_sys else self._filter.cmbSystem.currentData()
        cell_id = None if all_sys else self._filter.cmbCell.currentData()
        dt_from = self._filter.dtFrom.date().toPyDate()
        dt_to   = self._filter.dtTo.date().toPyDate()

        params: dict = {"dt_from": dt_from, "dt_to": dt_to, "spike_type": CELL_SPIKE}
        clauses = [
            "s.SampleType = :spike_type",
            "e.EnrichmentParam > 0",
            "e.EnrichmentParam IS NOT NULL",
            "er.RunEndTime::date >= :dt_from",
            "er.RunEndTime::date <= :dt_to",
        ]
        if sys_id is not None:
            clauses.append("eq.EquipmentID = :sys_id")
            params["sys_id"] = sys_id
        if cell_id is not None:
            clauses.append("e.CellID = :cell_id")
            params["cell_id"] = cell_id

        sql = text(f"""
            SELECT er.RunEndTime, eq.EquipmentName, e.CellID,
                   e.EnrichmentParam, e.EnrichmentParamUnc,
                   e.EnrichmentFactor, e.EnrichmentFactorUnc
            FROM Equipment eq
            JOIN TRIMS.ElectrolysisRun er ON eq.EquipmentID = er.ElysSystemID
            JOIN TRIMS.Electrolysis e     ON er.RunID = e.RunID
            JOIN Analysis a               ON e.AnalysisID = a.AnalysisID
            JOIN Sample s                 ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix
            WHERE eq.CategoryID = 2 AND {" AND ".join(clauses)}
            ORDER BY er.ElysSystemID, e.CellID, er.RunEndTime
        """)
        try:
            with db_manager.get_connection() as conn:
                self._rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            log.error("Spiked cells fetch: %s", exc)
            show_message(self, "Query Error", str(exc))
            self._rows = []
        self._update_display()

    def _update_display(self):
        rows = self._rows
        self._filter.clear_stats()
        self._chart.clear()
        self._table.populate([])
        self._outlier_indices = set()
        if not rows:
            return

        values  = [float(r.EnrichmentParam) for r in rows]
        uncs    = [float(r.EnrichmentParamUnc or 0) for r in rows]
        efs     = [float(r.EnrichmentFactor or 0) for r in rows]
        ef_uncs = [float(r.EnrichmentFactorUnc or 0) for r in rows]

        mean  = float(np.mean(values))
        stdev = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
        self._mean = mean
        self._stdev = stdev

        omask = _outlier_mask(values, mean, stdev)
        self._outlier_indices = {i for i, o in enumerate(omask) if o}
        status = _status_from_mask(omask)

        self._filter.set_stats(
            mean, _quadrature_mean_unc(values, uncs),
            float(np.mean(efs)) if efs else None,
            _quadrature_mean_unc(efs, ef_uncs),
            len(values), stdev, status
        )

        dt_dates = [r.RunEndTime if isinstance(r.RunEndTime, datetime)
                    else datetime.combine(r.RunEndTime, datetime.min.time())
                    for r in rows]
        self._chart.plot(dt_dates, values, mean, stdev, omask)

        table_rows = [
            (
                r.RunEndTime.strftime("%Y-%m-%d") if r.RunEndTime else "",
                r.EquipmentName or "", r.CellID,
                _fmt(r.EnrichmentParam), _fmt(r.EnrichmentParamUnc),
                _fmt(r.EnrichmentFactor), _fmt(r.EnrichmentFactorUnc),
                "⚠ outlier" if omask[i] else "",
            )
            for i, r in enumerate(rows)
        ]
        self._table.populate(table_rows, self._outlier_indices)

    def export_data(self):
        return self._TABLE_HEADERS, [
            (r.RunEndTime.strftime("%Y-%m-%d") if r.RunEndTime else "",
             r.EquipmentName or "", r.CellID,
             r.EnrichmentParam, r.EnrichmentParamUnc,
             r.EnrichmentFactor, r.EnrichmentFactorUnc, "")
            for r in self._rows
        ]

    def report_data(self):
        hdrs, rows = self.export_data()
        stats = {
            "Status": self._filter.badge.text(),
            "n": self._filter.txtN.text(),
            "Mean EP": self._filter.txtAvgEP.text(),
            "σ (EP)": self._filter.txtStdev.text(),
            "Avg EP Unc": self._filter.txtAvgEPUnc.text(),
            "Avg EF": self._filter.txtAvgEF.text(),
            "Avg EF Unc": self._filter.txtAvgEFUnc.text(),
        }
        f = self._filter
        all_sys = f.chkAllSystems.isChecked()
        parts = []
        if all_sys:
            parts.append("All Systems")
        else:
            parts.append(f"System: {f.cmbSystem.currentText()}")
            if f.cmbCell.currentText():
                parts.append(f"Cell: {f.cmbCell.currentText()}")
        parts.append(f"{f.dtFrom.date().toString('yyyy-MM-dd')} → {f.dtTo.date().toString('yyyy-MM-dd')}")
        filter_summary = "  ·  ".join(parts)
        return stats, hdrs, rows, self._outlier_indices, filter_summary


# ============================================================================
#  Page: Deuterium Recovery
# ============================================================================
class _DeuteriumPage(QWidget):
    _TABLE_HEADERS = ["Run Date", "System", "Cell", "2H Recovery", "EP", "EP Unc", "EF", "EF Unc", "Flag"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list = []
        self._outlier_indices: set = set()
        self._mean = 0.0
        self._stdev = 0.0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        top = QHBoxLayout()
        self._chart = _QaQcChart("Deuterium Recovery", self)
        top.addWidget(self._chart, stretch=1)

        self._filter = _ElysFilterPanel("Filter", self)
        self._filter.chkAllSystems.toggled.connect(self._on_all_systems)
        self._filter.cmbSystem.currentIndexChanged.connect(self._on_system_changed)
        self._filter.btnApply.clicked.connect(self.refresh)
        self._filter.btnNext.clicked.connect(self._next_cell)
        self._filter.btnPrev.clicked.connect(self._prev_cell)
        top.addWidget(self._filter)
        lay.addLayout(top, stretch=1)

        self._table = _DataTable(self._TABLE_HEADERS, self)
        lay.addWidget(self._table)

        self._load_systems()
        self.refresh()

    def _load_systems(self):
        self._filter.cmbSystem.blockSignals(True)
        self._filter.cmbSystem.clear()
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT e.EquipmentID, e.EquipmentName "
                    "FROM Equipment e "
                    "JOIN ElectrolysisSystem es ON e.EquipmentID = es.ElysSystemID "
                    "WHERE e.CategoryID = 2 ORDER BY e.EquipmentID"
                )).fetchall()
            for r in rows:
                self._filter.cmbSystem.addItem(
                    f"{r.EquipmentID} – {r.EquipmentName}", r.EquipmentID)
        except Exception as exc:
            log.warning("Deuterium: load systems: %s", exc)
        self._filter.cmbSystem.blockSignals(False)
        if self._filter.cmbSystem.count():
            self._on_system_changed()

    def _on_all_systems(self, checked):
        self._filter.cmbSystem.setEnabled(not checked)
        self._filter.cmbCell.setEnabled(not checked)
        self._filter.btnPrev.setEnabled(not checked)
        self._filter.btnNext.setEnabled(not checked)
        self.refresh()

    def _on_system_changed(self):
        sys_id = self._filter.cmbSystem.currentData()
        self._filter.cmbCell.blockSignals(True)
        self._filter.cmbCell.clear()
        if sys_id is not None:
            try:
                with db_manager.get_connection() as conn:
                    rows = conn.execute(text(
                        "SELECT CellID FROM ElectrolysisCell "
                        "WHERE SystemID = :sid ORDER BY CellID"
                    ), {"sid": sys_id}).fetchall()
                for r in rows:
                    self._filter.cmbCell.addItem(str(r.CellID), r.CellID)
            except Exception as exc:
                log.warning("Deuterium: load cells: %s", exc)
        self._filter.cmbCell.blockSignals(False)
        self.refresh()

    def _next_cell(self):
        cb = self._filter.cmbCell
        cb.setCurrentIndex((cb.currentIndex() + 1) % cb.count() if cb.count() else 0)
        self.refresh()

    def _prev_cell(self):
        cb = self._filter.cmbCell
        cb.setCurrentIndex((cb.currentIndex() - 1) % cb.count() if cb.count() else 0)
        self.refresh()

    def refresh(self):
        all_sys = self._filter.chkAllSystems.isChecked()
        sys_id  = None if all_sys else self._filter.cmbSystem.currentData()
        cell_id = None if all_sys else self._filter.cmbCell.currentData()
        dt_from = self._filter.dtFrom.date().toPyDate()
        dt_to   = self._filter.dtTo.date().toPyDate()

        params: dict = {"dt_from": dt_from, "dt_to": dt_to}
        clauses = [
            "de.EnrichmentFactor > 0",
            "de.EnrichmentFactor IS NOT NULL",
            "er.RunEndTime::date >= :dt_from",
            "er.RunEndTime::date <= :dt_to",
        ]
        if sys_id is not None:
            clauses.append("eq.EquipmentID = :sys_id"); params["sys_id"] = sys_id
        if cell_id is not None:
            clauses.append("e.CellID = :cell_id"); params["cell_id"] = cell_id

        sql = text(f"""
            SELECT er.RunEndTime, eq.EquipmentName, e.CellID,
                   e.EnrichmentParam, e.EnrichmentParamUnc,
                   de.DeuteriumRecovery, de.EnrichmentFactor, de.EnrichmentFactorUnc
            FROM Sample s
            JOIN Analysis a                   ON s.SampleID = a.SampleID AND s.Prefix = a.Prefix
            JOIN TRIMS.Electrolysis e         ON a.AnalysisID = e.AnalysisID
            JOIN TRIMS.ElectrolysisRun er     ON er.RunID = e.RunID
            JOIN TRIMS.DeuteriumEnrichment de ON e.ElectrolysisID = de.ElectrolysisID
            JOIN Equipment eq                 ON eq.EquipmentID = er.ElysSystemID AND eq.CategoryID = 2
            WHERE {" AND ".join(clauses)}
            ORDER BY er.ElysSystemID, e.CellID, er.RunEndTime
        """)
        try:
            with db_manager.get_connection() as conn:
                self._rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            log.error("Deuterium fetch: %s", exc)
            show_message(self, "Query Error", str(exc))
            self._rows = []
        self._update_display()

    def _update_display(self):
        rows = self._rows
        self._filter.clear_stats()
        self._chart.clear()
        self._table.populate([])
        self._outlier_indices = set()
        if not rows:
            return

        ep_vals  = [float(r.EnrichmentParam or 0) for r in rows]
        ep_uncs  = [float(r.EnrichmentParamUnc or 0) for r in rows]
        dr_vals  = [float(r.DeuteriumRecovery) if r.DeuteriumRecovery is not None else None
                    for r in rows]
        ef_vals  = [float(r.EnrichmentFactor) for r in rows if r.EnrichmentFactor is not None]
        ef_uncs  = [float(r.EnrichmentFactorUnc or 0) for r in rows if r.EnrichmentFactor is not None]

        valid_dr = [v for v in dr_vals if v is not None]
        if not valid_dr:
            return

        mean  = float(np.mean(valid_dr))
        stdev = float(np.std(valid_dr, ddof=1)) if len(valid_dr) > 1 else 0.0
        self._mean = mean
        self._stdev = stdev

        omask = [_outlier_mask([v], mean, stdev)[0] if v is not None else False
                 for v in dr_vals]
        self._outlier_indices = {i for i, o in enumerate(omask) if o}
        status = _status_from_mask(omask)

        self._filter.set_stats(
            mean, _quadrature_mean_unc(valid_dr, ep_uncs[:len(valid_dr)]),
            float(np.mean(ef_vals)) if ef_vals else None,
            _quadrature_mean_unc(ef_vals, ef_uncs),
            len(valid_dr), stdev, status
        )

        dt_dates = [r.RunEndTime if isinstance(r.RunEndTime, datetime)
                    else datetime.combine(r.RunEndTime, datetime.min.time())
                    for r in rows]
        valid_pairs = [(d, v, o) for d, v, o in zip(dt_dates, dr_vals, omask)
                       if v is not None]
        if valid_pairs:
            vd, vv, vo = zip(*valid_pairs)
            self._chart.plot(list(vd), list(vv), mean, stdev, list(vo))

        table_rows = [
            (
                r.RunEndTime.strftime("%Y-%m-%d") if r.RunEndTime else "",
                r.EquipmentName or "", r.CellID,
                _fmt(r.DeuteriumRecovery), _fmt(r.EnrichmentParam),
                _fmt(r.EnrichmentParamUnc), _fmt(r.EnrichmentFactor),
                _fmt(r.EnrichmentFactorUnc),
                "⚠ outlier" if omask[i] else "",
            )
            for i, r in enumerate(rows)
        ]
        self._table.populate(table_rows, self._outlier_indices)

    def export_data(self):
        return self._TABLE_HEADERS, [
            (r.RunEndTime.strftime("%Y-%m-%d") if r.RunEndTime else "",
             r.EquipmentName or "", r.CellID,
             r.DeuteriumRecovery, r.EnrichmentParam, r.EnrichmentParamUnc,
             r.EnrichmentFactor, r.EnrichmentFactorUnc, "")
            for r in self._rows
        ]

    def report_data(self):
        hdrs, rows = self.export_data()
        stats = {
            "Status": self._filter.badge.text(),
            "n": self._filter.txtN.text(),
            "Mean 2H Recovery": self._filter.txtAvgEP.text(),
            "σ (Recovery)": self._filter.txtStdev.text(),
            "Avg EF": self._filter.txtAvgEF.text(),
            "Avg EF Unc": self._filter.txtAvgEFUnc.text(),
        }
        f = self._filter
        all_sys = f.chkAllSystems.isChecked()
        parts = ["All Systems"] if all_sys else [f"System: {f.cmbSystem.currentText()}"]
        if not all_sys and f.cmbCell.currentText():
            parts.append(f"Cell: {f.cmbCell.currentText()}")
        parts.append(f"{f.dtFrom.date().toString('yyyy-MM-dd')} → {f.dtTo.date().toString('yyyy-MM-dd')}")
        return stats, hdrs, rows, self._outlier_indices, "  ·  ".join(parts)


# ============================================================================
#  Page: LS Counter / Lab Air / Control Sample
# ============================================================================
class _LSCounterPage(QWidget):
    _TABLE_HEADERS = [
        "Run Date", "Counter", "Run ID", "Net CPM", "CPM Unc",
        "Efficiency %", "Background CPM", "Calibration Factor", "Flag"
    ]

    def __init__(self, preset_standard_id: Optional[int] = None, parent=None):
        super().__init__(parent)
        self._rows: list = []
        self._outlier_indices: set = set()
        self._preset_standard = preset_standard_id
        self._mean = 0.0
        self._stdev = 0.0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        top = QHBoxLayout()
        self._chart = _QaQcChart("Net CPM (decay-corrected)", self)
        top.addWidget(self._chart, stretch=1)

        self._filter = _LSCFilterPanel(self)
        self._filter.chkAllCounters.toggled.connect(self._on_all_counters)
        self._filter.cmbCounter.currentIndexChanged.connect(self.refresh)
        self._filter.cmbStandard.currentIndexChanged.connect(self.refresh)
        self._filter.btnApply.clicked.connect(self.refresh)
        top.addWidget(self._filter)
        lay.addLayout(top, stretch=1)

        self._table = _DataTable(self._TABLE_HEADERS, self)
        lay.addWidget(self._table)

        self._load_counters()
        self._load_standards()
        if self._preset_standard is not None:
            self._set_preset_standard()
        self.refresh()

    def _load_counters(self):
        self._filter.cmbCounter.blockSignals(True)
        self._filter.cmbCounter.clear()
        self._filter.cmbCounter.addItem("— All —", None)
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT EquipmentID, EquipmentName FROM Equipment "
                    "WHERE CategoryID = 3 ORDER BY EquipmentID"
                )).fetchall()
            for r in rows:
                self._filter.cmbCounter.addItem(
                    f"{r.EquipmentID} – {r.EquipmentName}", r.EquipmentID)
        except Exception as exc:
            log.warning("LSC: load counters: %s", exc)
        self._filter.cmbCounter.blockSignals(False)

    def _load_standards(self):
        self._filter.cmbStandard.blockSignals(True)
        self._filter.cmbStandard.clear()
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT DISTINCT s.SampleID, s.sName "
                    "FROM Sample s "
                    "JOIN Analysis a ON s.SampleID = a.SampleID AND s.Prefix = a.Prefix "
                    "JOIN TRIMS.LSCLoadList ll ON a.AnalysisID = ll.AnalysisID "
                    "WHERE s.SampleType IN (1, 2, 6) ORDER BY s.SampleID"
                )).fetchall()
            for r in rows:
                self._filter.cmbStandard.addItem(
                    f"{r.SampleID} – {r.sName or ''}", r.SampleID)
        except Exception as exc:
            log.warning("LSC: load standards: %s", exc)
        self._filter.cmbStandard.blockSignals(False)

    def _set_preset_standard(self):
        cb = self._filter.cmbStandard
        for i in range(cb.count()):
            if cb.itemData(i) == self._preset_standard:
                cb.setCurrentIndex(i)
                return

    def _on_all_counters(self, checked):
        self._filter.cmbCounter.setEnabled(not checked)
        self.refresh()

    def refresh(self):
        all_ctrs = self._filter.chkAllCounters.isChecked()
        equip_id = None if all_ctrs else self._filter.cmbCounter.currentData()
        std_id   = self._filter.cmbStandard.currentData()
        dt_from  = self._filter.dtFrom.date().toPyDate()
        dt_to    = self._filter.dtTo.date().toPyDate()

        if std_id is None:
            self._chart.clear(); self._table.populate([]); self._filter.clear_stats()
            return

        params: dict = {"std_id": std_id, "dt_from": dt_from, "dt_to": dt_to}
        clauses = [
            "lr.FinalActivity IS NOT NULL", "lr.RejectFlag = 0",
            "s.SampleID = :std_id", "lr.FinalActivity <> 0",
            "rm.ValueKind = 1",
            "r.RunEndTime::date >= :dt_from", "r.RunEndTime::date <= :dt_to",
        ]
        if equip_id is not None:
            clauses.append("r.EquipmentID = :equip_id")
            params["equip_id"] = equip_id

        sql = text(f"""
            SELECT r.RunEndTime, eq.EquipmentName, r.RunID,
                   rm.MeanValue AS net_cpm, rm.MeanValueUnc AS net_cpm_unc,
                   r.CounterEfficiency, r.CounterEfficiencyUnc,
                   r.MeanBackground, r.MeanBackgroundUnc,
                   r.CalibrationFactor, r.CalibrationFactorUnc
            FROM TRIMS.LSCRun r
            JOIN Equipment eq             ON eq.EquipmentID = r.EquipmentID
            JOIN TRIMS.LSCLoadList ll     ON r.RunID = ll.RunID
            JOIN TRIMS.LSCResult lr       ON ll.AnalysisID = lr.AnalysisID AND ll.RunID = lr.RunID
            JOIN TRIMS.LSCRunMean rm      ON ll.CountID = rm.CountID AND rm.ValueKind = 1
            JOIN Analysis a               ON a.AnalysisID = ll.AnalysisID
            JOIN Sample s                 ON s.SampleID = a.SampleID AND s.Prefix = a.Prefix
            WHERE {" AND ".join(clauses)}
            ORDER BY r.RunEndTime
        """)
        try:
            with db_manager.get_connection() as conn:
                self._rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            log.error("LSC QaQC fetch: %s", exc)
            show_message(self, "Query Error", str(exc))
            self._rows = []
        self._update_display()

    def _update_display(self):
        rows = self._rows
        self._filter.clear_stats()
        self._chart.clear()
        self._table.populate([])
        self._outlier_indices = set()
        if not rows:
            return

        net_cpms = [float(r.net_cpm) for r in rows if r.net_cpm is not None]
        if not net_cpms:
            return

        mean  = float(np.mean(net_cpms))
        stdev = float(np.std(net_cpms, ddof=1)) if len(net_cpms) > 1 else 0.0
        self._mean = mean
        self._stdev = stdev
        n = len(net_cpms)

        omask = _outlier_mask(net_cpms, mean, stdev)
        self._outlier_indices = {i for i, o in enumerate(omask) if o}
        status = _status_from_mask(omask)

        cpm_uncs = [float(r.net_cpm_unc or 0) for r in rows if r.net_cpm is not None]
        effs = [float(r.CounterEfficiency) for r in rows if r.CounterEfficiency is not None]
        eff_uncs = [float(r.CounterEfficiencyUnc or 0) for r in rows if r.CounterEfficiency is not None]
        bkgs = [float(r.MeanBackground) for r in rows if r.MeanBackground is not None]
        bkg_uncs = [float(r.MeanBackgroundUnc or 0) for r in rows if r.MeanBackground is not None]
        calibs = [float(r.CalibrationFactor) for r in rows if r.CalibrationFactor is not None]
        calib_uncs = [float(r.CalibrationFactorUnc or 0) for r in rows if r.CalibrationFactor is not None]

        avg_eff = 100 * float(np.mean(effs)) if effs else None
        avg_bkg = float(np.mean(bkgs)) if bkgs else None
        avg_bkg_unc = _quadrature_mean_unc(bkgs, bkg_uncs)
        avg_calib = float(np.mean(calibs)) if calibs else None
        avg_calib_unc = _quadrature_mean_unc(calibs, calib_uncs)
        fom = (avg_eff ** 2) / avg_bkg if avg_eff and avg_bkg else None

        self._filter.txtAvgCPM.setText(f"{_fmt(mean)} ± {_fmt(_quadrature_mean_unc(net_cpms, cpm_uncs))}")
        self._filter.txtAvgEff.setText(_fmt(avg_eff, 1) if avg_eff is not None else "—")
        self._filter.txtBkg.setText(f"{_fmt(avg_bkg)} ± {_fmt(avg_bkg_unc)}" if avg_bkg else "—")
        self._filter.txtCalib.setText(f"{_fmt(avg_calib)} ± {_fmt(avg_calib_unc)}" if avg_calib else "—")
        self._filter.txtFOM.setText(_fmt(fom, 1) if fom else "—")
        self._filter.txtN.setText(str(n))
        self._filter.txtStdev.setText(_fmt(stdev))
        self._filter.badge.set_status(status)

        dt_dates = [r.RunEndTime if isinstance(r.RunEndTime, datetime)
                    else datetime.combine(r.RunEndTime, datetime.min.time())
                    for r in rows]
        self._chart.plot(dt_dates, net_cpms, mean, stdev, omask)

        table_rows = [
            (
                r.RunEndTime.strftime("%Y-%m-%d") if r.RunEndTime else "",
                r.EquipmentName or "", r.RunID,
                _fmt(r.net_cpm), _fmt(r.net_cpm_unc),
                _fmt((r.CounterEfficiency or 0) * 100, 2),
                _fmt(r.MeanBackground), _fmt(r.CalibrationFactor),
                "⚠ outlier" if omask[i] else "",
            )
            for i, r in enumerate(rows)
        ]
        self._table.populate(table_rows, self._outlier_indices)

    def export_data(self):
        return self._TABLE_HEADERS, [
            (r.RunEndTime.strftime("%Y-%m-%d") if r.RunEndTime else "",
             r.EquipmentName or "", r.RunID,
             r.net_cpm, r.net_cpm_unc,
             (r.CounterEfficiency or 0) * 100,
             r.MeanBackground, r.CalibrationFactor, "")
            for r in self._rows
        ]

    def report_data(self):
        hdrs, rows = self.export_data()
        f = self._filter
        stats = {
            "Status":       f.badge.text(),
            "n":            f.txtN.text(),
            "Avg Net CPM":  f.txtAvgCPM.text(),
            "σ (CPM)":      f.txtStdev.text(),
            "Efficiency %": f.txtAvgEff.text(),
            "Background":   f.txtBkg.text(),
            "Calibration":  f.txtCalib.text(),
            "FOM":          f.txtFOM.text(),
        }
        all_ctrs = f.chkAllCounters.isChecked()
        parts = ["All Counters"] if all_ctrs else [f"Counter: {f.cmbCounter.currentText()}"]
        parts.append(f"Standard: {f.cmbStandard.currentText()}")
        parts.append(f"{f.dtFrom.date().toString('yyyy-MM-dd')} → {f.dtTo.date().toString('yyyy-MM-dd')}")
        return stats, hdrs, rows, self._outlier_indices, "  ·  ".join(parts)


# ============================================================================
#  Main QA/QC window
# ============================================================================
class QaQcWindow(QWidget):
    # (label, stack_index_or_None)  None = separator, not selectable
    _MODES = [
        ("── TRIMS ──",             None),
        ("  Spiked Cells",          0),
        ("  Deuterium Recovery",    1),
        ("  LS Counter",            2),
        ("  Lab Air Moisture",      3),
        ("  Control Sample",        4),
        ("── SIAM ──",              None),
        ("  Reference Standard",    5),
        ("── NGAM ──",              None),
        ("  3He Ingrowth",          6),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("IsoWorks :: QA/QC Module")
        self.resize(1200, 780)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────────────
        hdr = QWidget()
        hdr.setFixedHeight(44)
        hdr.setStyleSheet("background:#00695C;")
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(12, 4, 12, 4)

        lbl_title = QLabel("IsoWorks :: QA/QC")
        lbl_title.setStyleSheet("color:white;font-size:14pt;font-weight:bold;")
        hdr_lay.addWidget(lbl_title)
        hdr_lay.addStretch()

        lbl_param = QLabel("Parameter:")
        lbl_param.setStyleSheet("color:white;font-size:11pt;font-weight:bold;")
        hdr_lay.addWidget(lbl_param)

        self.cmbMode = QComboBox()
        self.cmbMode.setMinimumWidth(220)
        self.cmbMode.setStyleSheet("QComboBox{font-size:11pt;padding:2px 6px;}")

        # Populate with separators as disabled items
        from PyQt5.QtGui import QStandardItem
        from PyQt5.QtWidgets import QSizePolicy as _QSP
        model = self.cmbMode.model()
        for label, idx in self._MODES:
            item = QStandardItem(label)
            if idx is None:
                item.setEnabled(False)
                item.setForeground(QColor("#AAAAAA"))
                f = item.font(); f.setItalic(True); item.setFont(f)
            model.appendRow(item)

        hdr_lay.addWidget(self.cmbMode)
        hdr_lay.addSpacing(12)

        _btn_style = (
            "QPushButton{color:white;background:transparent;border:1px solid white;"
            "border-radius:4px;padding:4px 10px;font-size:10pt;}"
            "QPushButton:hover{background:rgba(255,255,255,30);}"
        )
        self.btnExport = QPushButton("⬇ Export CSV")
        self.btnExport.setStyleSheet(_btn_style)
        hdr_lay.addWidget(self.btnExport)

        self.btnReport = QPushButton("📄 Report")
        self.btnReport.setStyleSheet(_btn_style)
        hdr_lay.addWidget(self.btnReport)

        root.addWidget(hdr)

        # ── Pages  (stack index must match _MODES values) ────────────────────
        self._stack = QStackedWidget()
        self._page_spiked    = _SpikedCellsPage(self)        # 0
        self._page_deuterium = _DeuteriumPage(self)           # 1
        self._page_lsc       = _LSCounterPage(parent=self)    # 2
        self._page_lab_air   = _LSCounterPage(preset_standard_id=400, parent=self)  # 3
        self._page_control   = _LSCounterPage(preset_standard_id=500, parent=self)  # 4
        self._page_siam      = _SiamStandardPage(self)        # 5
        self._page_ngam      = _NgamPlaceholderPage(self)     # 6

        for page in [self._page_spiked, self._page_deuterium,
                     self._page_lsc, self._page_lab_air, self._page_control,
                     self._page_siam, self._page_ngam]:
            self._stack.addWidget(page)
        root.addWidget(self._stack)

        # ── Status bar ───────────────────────────────────────────────────────
        self._statusbar = QLabel("")
        self._statusbar.setStyleSheet(
            "background:#E0F2F1;color:#00695C;padding:2px 8px;font-size:9pt;")
        root.addWidget(self._statusbar)

        self.cmbMode.currentIndexChanged.connect(self._on_mode_changed)
        self.btnExport.clicked.connect(self._export)
        self.btnReport.clicked.connect(self._report)

        # Start on LS Counter (first non-separator with stack idx 2)
        for row, (_, stack_idx) in enumerate(self._MODES):
            if stack_idx == 2:
                self.cmbMode.setCurrentIndex(row)
                break

    def _on_mode_changed(self, row: int):
        _, stack_idx = self._MODES[row]
        if stack_idx is None:
            # User somehow landed on a separator — move to next real item
            for next_row in range(row + 1, len(self._MODES)):
                if self._MODES[next_row][1] is not None:
                    self.cmbMode.setCurrentIndex(next_row)
                    return
            return
        self._stack.setCurrentIndex(stack_idx)

    def _current_page(self):
        return self._stack.currentWidget()

    # ── CSV Export ───────────────────────────────────────────────────────────
    def _export(self):
        page = self._current_page()
        if not hasattr(page, "export_data"):
            return
        headers, rows = page.export_data()
        if not rows:
            show_message(self, "Export", "No data to export.")
            return
        mode_name = self.cmbMode.currentText().strip().replace(" ", "_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export QA/QC Data",
            f"QAQC_{mode_name}_{ts}.csv",
            "CSV Files (*.csv);;All Files (*)"
        )
        if not path:
            return
        try:
            import csv
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(headers)
                csv.writer(f).writerows(rows)
            self._statusbar.setText(f"Exported {len(rows)} rows → {path}")
        except Exception as exc:
            show_message(self, "Export Error", str(exc))

    # ── HTML Report ──────────────────────────────────────────────────────────
    def _report(self):
        page = self._current_page()
        if not hasattr(page, "report_data"):
            return

        stats, headers, rows, outlier_indices, filter_summary = page.report_data()

        if not rows:
            show_message(self, "Report", "No data — apply filters first.")
            return

        # Render chart to PNG
        try:
            png_bytes = page._chart.to_png_bytes()
        except Exception as exc:
            log.warning("Chart PNG render failed: %s", exc)
            png_bytes = b""

        mode_title = self.cmbMode.currentText().strip()
        html = _build_html_report(
            mode_title, filter_summary,
            stats, headers, rows, outlier_indices, png_bytes
        )

        # Write to temp file and open in browser
        fd, path = tempfile.mkstemp(
            prefix=f"IsoWorks_QaQC_{mode_title.replace(' ','_')}_",
            suffix=".html"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(html)
            webbrowser.open(f"file://{path}")
            self._statusbar.setText(f"Report opened: {path}")
        except Exception as exc:
            show_message(self, "Report Error", str(exc))

    def status(self, msg: str):
        self._statusbar.setText(msg)


# ============================================================================
#  SIAM filter panel
# ============================================================================
class _SiamFilterPanel(_FilterPanelBase):
    def __init__(self, parent=None):
        super().__init__("Filters", parent)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(3)

        self.chkAllInstruments = QCheckBox("All Instruments")
        self.chkAllInstruments.setChecked(True)
        self._outer.addWidget(self.chkAllInstruments)

        self.cmbInstrument = QComboBox()
        self.cmbInstrument.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        form.addRow("Instrument:", self.cmbInstrument)

        self.cmbMeasurable = QComboBox()
        self.cmbMeasurable.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        form.addRow("Measurable:", self.cmbMeasurable)

        self.cmbStandard = QComboBox()
        self.cmbStandard.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        form.addRow("Standard:", self.cmbStandard)

        self._outer.addLayout(form)

        self.dtFrom, self.dtTo = self._build_date_block()
        self.btnApply = self._build_apply_button()
        self._build_separator()
        self.badge = self._build_status_badge()
        self._build_separator()

        lbl_stats = QLabel("Statistics")
        lbl_stats.setFont(QFont("Arial", 9, QFont.Bold))
        lbl_stats.setStyleSheet("color:#1565C0;")
        self._outer.addWidget(lbl_stats)

        sf = QFormLayout()
        sf.setLabelAlignment(Qt.AlignRight)
        sf.setSpacing(3)
        self.txtN       = QLineEdit(); self.txtN.setReadOnly(True)
        self.txtMean    = QLineEdit(); self.txtMean.setReadOnly(True)
        self.txtStdev   = QLineEdit(); self.txtStdev.setReadOnly(True)
        self.txtCertif  = QLineEdit(); self.txtCertif.setPlaceholderText("optional")
        self.txtBias    = QLineEdit(); self.txtBias.setReadOnly(True)
        for w in (self.txtN, self.txtMean, self.txtStdev, self.txtCertif, self.txtBias):
            w.setFixedHeight(22)
        sf.addRow("n:",         self.txtN)
        sf.addRow("Mean:",      self.txtMean)
        sf.addRow("σ:",         self.txtStdev)
        sf.addRow("Certified:", self.txtCertif)
        sf.addRow("Bias:",      self.txtBias)
        self._outer.addLayout(sf)
        self._build_separator()
        self._build_legend()

        self.chkAllInstruments.toggled.connect(
            lambda on: self.cmbInstrument.setEnabled(not on))


# ============================================================================
#  SIAM reference standard page
# ============================================================================
class _SiamStandardPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._outlier_indices: List[int] = []
        self._rows: list = []
        self._headers: list = []

        main = QVBoxLayout(self)
        main.setContentsMargins(4, 4, 4, 4)
        main.setSpacing(4)

        body = QHBoxLayout()
        self._chart = _QaQcChart(y_label="δ value")
        body.addWidget(self._chart, 3)

        self._filter = _SiamFilterPanel()
        self._filter.btnApply.clicked.connect(self.refresh)
        self._filter.txtCertif.editingFinished.connect(self._recompute_bias)
        body.addWidget(self._filter, 1)

        main.addLayout(body, 3)

        self._table = _DataTable(headers=[])
        main.addWidget(self._table, 1)

        self._load_combos()
        self.refresh()

    # ── combo population ──────────────────────────────────────────────────────
    def _load_combos(self):
        f = self._filter
        try:
            with db_manager.get_connection() as conn:
                # IRMS instruments (CategoryID = 5)
                rows = conn.execute(text(
                    "SELECT EquipmentID, EquipmentName FROM Equipment "
                    "WHERE CategoryID = 5 ORDER BY EquipmentID"
                )).fetchall()
                f.cmbInstrument.clear()
                for r in rows:
                    f.cmbInstrument.addItem(r.EquipmentName, r.EquipmentID)

                # Measurables present in SIAnalysisResult
                rows = conn.execute(text("""
                    SELECT DISTINCT m.AnalyteID AS MeasurableID, m.AnalyteName AS MeasurableName
                    FROM Analytes m
                    WHERE EXISTS (
                        SELECT 1 FROM SIAM.SIAnalysisResult r
                        WHERE r.MeasurableID = m.AnalyteID
                    )
                    ORDER BY m.MeasurableName
                """)).fetchall()
                f.cmbMeasurable.clear()
                for r in rows:
                    f.cmbMeasurable.addItem(r.MeasurableName, r.MeasurableID)

                # Reference standards (SampleType IN (2,3,4))
                rows = conn.execute(text("""
                    SELECT DISTINCT s.SampleID, s.Prefix, s.sName
                    FROM Sample s
                    WHERE s.SampleType IN (2, 3, 4)
                      AND EXISTS (
                          SELECT 1 FROM Analysis a
                          JOIN SIAM.SIAnalysisLoadList ll ON ll.AnalysisID = a.AnalysisID
                          WHERE a.SampleID = s.SampleID AND a.Prefix = s.Prefix
                          AND ll.SIAnalysisRunID IS NOT NULL
                      )
                    ORDER BY s.sName
                """)).fetchall()
                f.cmbStandard.clear()
                for r in rows:
                    label = f"{r.Prefix}-{r.SampleID}: {r.sName or ''}"
                    f.cmbStandard.addItem(label, (r.Prefix, r.SampleID))
        except Exception as exc:
            log.warning("SIAM combo load failed: %s", exc)

    # ── data fetch ────────────────────────────────────────────────────────────
    def refresh(self):
        f = self._filter
        clauses = []
        params: dict = {}

        all_instr = f.chkAllInstruments.isChecked()
        if not all_instr and f.cmbInstrument.currentData() is not None:
            clauses.append("r.EquipmentID = :eid")
            params["eid"] = f.cmbInstrument.currentData()

        mid = f.cmbMeasurable.currentData()
        if mid is not None:
            clauses.append("res.MeasurableID = :mid")
            params["mid"] = mid

        std = f.cmbStandard.currentData()
        if std is not None:
            clauses.append("s.Prefix = :pfx AND s.SampleID = :sid")
            params["pfx"], params["sid"] = std

        d_from = f.dtFrom.date().toPyDate()
        d_to   = f.dtTo.date().toPyDate()
        clauses.append("r.RunStartTime::date >= :df AND r.RunStartTime::date <= :dt")
        params["df"] = d_from
        params["dt"] = d_to

        where = "WHERE s.SampleType IN (2, 3, 4)"
        if clauses:
            where += " AND " + " AND ".join(clauses)

        sql = text(f"""
            SELECT
                r.RunStartTime::date                    AS run_date,
                r.SIAnalysisRunID                       AS run_id,
                e.EquipmentName                         AS instrument,
                s.sName                                 AS std_name,
                m.AnalyteName                            AS measurable,
                res.fValue                              AS value,
                res.fValueUnc                           AS unc
            FROM SIAM.SIAnalysisResult   res
            JOIN SIAM.SIAnalysisLoadList  ll ON ll.SIAnalysisID    = res.SIAnalysisID
            JOIN SIAM.SIAnalysisRun       r  ON r.SIAnalysisRunID  = ll.SIAnalysisRunID
            JOIN Analysis                 a  ON a.AnalysisID       = ll.AnalysisID
            JOIN Sample                   s  ON s.SampleID         = a.SampleID
                                            AND s.Prefix           = a.Prefix
            JOIN Analytes                 m  ON m.AnalyteID        = res.MeasurableID
            LEFT JOIN Equipment           e  ON e.EquipmentID      = r.EquipmentID
            {where}
            ORDER BY r.RunStartTime, r.SIAnalysisRunID
        """)

        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            log.error("SIAM QaQc fetch: %s", exc)
            show_message(self, "Query Error", str(exc))
            return

        self._update_display(rows)

    def _update_display(self, rows):
        f = self._filter

        if not rows:
            f.txtN.setText("0"); f.txtMean.setText("—")
            f.txtStdev.setText("—"); f.txtBias.setText("—")
            f.badge.set_status("NONE")
            self._chart.clear()
            self._table.populate([], [])
            self._rows = []; self._headers = []
            self._outlier_indices = []
            return

        dt_dates = [r.run_date for r in rows]
        values   = [float(r.value) for r in rows if r.value is not None]

        if not values:
            return

        mean  = float(np.mean(values))
        stdev = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
        omask = _outlier_mask(values, mean, stdev)

        f.txtN.setText(str(len(values)))
        meas_lbl = f.cmbMeasurable.currentText()
        f.txtMean.setText(f"{_fmt(mean, 3)} ‰")
        f.txtStdev.setText(f"{_fmt(stdev, 3)} ‰")
        f.badge.set_status(_status_from_mask(omask))

        self._recompute_bias(mean)

        self._outlier_indices = [i for i, v in enumerate(omask) if v]
        self._chart.ax.set_ylabel(f"{meas_lbl} (‰)", fontsize=9)
        self._chart.canvas.draw_idle()
        self._chart.plot(dt_dates, values, mean, stdev, omask)

        hdrs = ["Date", "RunID", "Instrument", "Standard", "Measurable", "Value (‰)", "Unc"]
        tbl_rows = [
            (str(r.run_date), str(r.run_id), r.instrument or "",
             r.std_name or "", r.measurable or "",
             _fmt(r.value, 3), _fmt(r.unc, 4))
            for r in rows
        ]
        self._headers = hdrs
        self._rows    = tbl_rows
        self._table.populate(tbl_rows, self._outlier_indices, headers=hdrs)

    def _recompute_bias(self, mean=None):
        f = self._filter
        cert_txt = f.txtCertif.text().strip()
        if not cert_txt:
            f.txtBias.setText("—")
            return
        try:
            cert = float(cert_txt)
            if mean is None:
                mean_txt = f.txtMean.text().replace("‰", "").strip()
                mean = float(mean_txt) if mean_txt and mean_txt != "—" else None
            if mean is not None:
                bias = mean - cert
                f.txtBias.setText(f"{_fmt(bias, 3)} ‰")
        except ValueError:
            f.txtBias.setText("—")

    def export_data(self):
        return self._headers, self._rows

    def report_data(self):
        f = self._filter
        stats = {
            "n":          f.txtN.text(),
            "Mean":       f.txtMean.text(),
            "σ":          f.txtStdev.text(),
            "Certified":  f.txtCertif.text() or "—",
            "Bias":       f.txtBias.text(),
            "Measurable": f.cmbMeasurable.currentText(),
            "Standard":   f.cmbStandard.currentText(),
        }
        all_instr = f.chkAllInstruments.isChecked()
        parts = ["All Instruments" if all_instr else f"Instrument: {f.cmbInstrument.currentText()}"]
        parts.append(f"Measurable: {f.cmbMeasurable.currentText()}")
        parts.append(f"Standard: {f.cmbStandard.currentText()}")
        parts.append(f"{f.dtFrom.date().toString('yyyy-MM-dd')} → {f.dtTo.date().toString('yyyy-MM-dd')}")
        return stats, self._headers, self._rows, self._outlier_indices, "  ·  ".join(parts)


# ============================================================================
#  NGAM placeholder page
# ============================================================================
class _NgamPlaceholderPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)

        icon = QLabel("🔬")
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet("font-size:48pt;")
        lay.addWidget(icon)

        msg = QLabel(
            "<b>NGAM QA/QC — Coming Soon</b><br><br>"
            "Planned metrics:<br>"
            "• 3He Ingrowth — Standard yield tracking<br>"
            "• MS Sequence — Reference gas stability<br>"
            "• Extraction — Blank & recovery trends"
        )
        msg.setAlignment(Qt.AlignCenter)
        msg.setStyleSheet("color:#546E7A;font-size:11pt;line-height:1.6;")
        msg.setTextFormat(Qt.RichText)
        lay.addWidget(msg)

    def export_data(self):
        return [], []

    def report_data(self):
        return {}, [], [], [], "NGAM — not yet implemented"


# ============================================================================
#  Main QA/QC window
# ============================================================================
# (replaces the existing QaQcWindow below — delete the old one)

# ============================================================================
#  Entry point
# ============================================================================
def open_qaqc_window(parent=None) -> QaQcWindow:
    w = QaQcWindow(parent)
    w.show()
    return w


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    win = QaQcWindow()
    win.show()
    sys.exit(app.exec_())
