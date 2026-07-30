"""
ngam_results_widget.py
======================
Shared, interactive Noble Gas processing-results display widget.

Works with any ProtocolSequence + SequenceProcessingResult pair, whether the
data originated from a NobleControl .protocol file or a Helix SFT XLSM
(converted via ngam_processing_bridge.helix_to_protocol_sequence).

Features
--------
- Inlet list (left) — click to select; type colour coding
- Chart (top right) — raw signal points + dashed fit lines; outlier markers
- Device signal tabs — per-action signal rows with interactive Outlier
  checkboxes.  Checking/unchecking a box overrides the auto-detected flag,
  refits the block and re-runs the full pipeline.
- Results tab — per-inlet isotope table (meas → bg → net → BC → ccSTP)
- Summary tab — sequence blank means and sensitivities
- "Reset Outliers" button — discards all user overrides and re-runs auto
"""
from __future__ import annotations

import math
import logging
import os
from typing import Dict, List, Optional, Set, Tuple

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QSplitter, QTabWidget, QSizePolicy,
    QLabel, QComboBox, QApplication, QDoubleSpinBox, QCheckBox,
    QTreeWidget, QTreeWidgetItem, QTextBrowser, QShortcut,
    QMenu, QAction,
)
from PyQt5.QtCore import Qt, pyqtSignal, QModelIndex
from PyQt5.QtGui import QColor, QFont, QKeySequence

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from plot_utils import attach_hover_tooltip

from ngam_protocol_parser import ProtocolSequence, InletPrep
from ngam_protocol_processor import (
    process_sequence, ProcessingConfig,
    SequenceProcessingResult, InletProcessingResult,
    DriftFit, BlankFit, LinearityFit,
    _is_background, _polyval, parse_unc_trace_lines,
)
from ngam_gauge_processor import (
    GaugeSequenceSummary, InletGaugeSummary, ChannelSummary, ChannelFit,
    PRIMARY_CHANNELS, SECONDARY_CHANNELS, ALL_CHANNELS,
)
from ngam_sms_parser import parse_sms
from ngam_signal_fitter import fit_inlet, MODELS as SMS_FIT_MODELS, fit_qms_isotope, compute_qms_ratios
from ngam_signal_fit_widget import SMSFitPanel
from ngam_qms_parser import parse_qms
from ngam_qms_fit_widget import QMSFitWidget
from ngam_inlet_signals_widget import InletSignalsWidget

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_DEVICES = ("SMS", "QMSNe", "QMSAr", "QMSKrXe")

# Action prefixes that are instrument intermediate phases, not reportable signals.
_INTERMEDIATE_PREFIXES = ("peakcenter", "peakraw", "pumpdown", "scan", "inlet")

def _is_reportable_action(action: str) -> bool:
    """Return True only for genuine signal actions (3He, 4He, 20Ne, …)."""
    al = action.lower()
    return (
        not any(al.startswith(p) for p in _INTERMEDIATE_PREFIXES)
        and "background" not in al
    )
_OUTLIER_COL = 5   # column index for the Outlier checkbox in signal tables

_HDR_SS = (
    "QHeaderView::section {"
    "  background-color: #3D6A9E; color: white; font-weight: bold;"
    "  padding: 3px 6px; border: none; border-right: 1px solid #2A4F7C; }"
    "QHeaderView::section:last-section { border-right: none; }"
)
_TABLE_SS = (
    "QTableWidget { border: 1px solid #C5CDD9; background: white;"
    "  gridline-color: transparent; alternate-background-color: #F0F4F8; }"
    "QTableWidget::item { padding: 3px 5px; }"
    "QTableWidget::item:selected { background: #BBDEFB; color: #000; }"
)
_BTN_RESET = (
    "QPushButton { background: #BF360C; color: white; font-weight: bold;"
    "  border: none; padding: 4px 12px; border-radius: 4px; }"
    "QPushButton:hover { background: #8D2000; }"
    "QPushButton:disabled { background: #B0BEC5; color: #78909C; }"
)
_DILUTION_COL = 4  # column index for dilution factor (Df) in inlet tree

_TYPE_BG = {
    "blank":    QColor("#E3F2FD"),
    "standard": QColor("#F3E5F5"),
    "sample":   QColor("#FFFFFF"),
}
_COL_SUPPLEMENTAL = QColor("#FFF9C4")   # pale yellow for supplemental inlets
_COL_OUTLIER_ROW = QColor("#FFCCBC")
_COL_OK   = QColor("#C8E6C9")
_COL_MISS = QColor("#FFCDD2")

_ACTION_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
    "#bcbd22", "#17becf",
]


def _popup_launcher(label: str, callback) -> QWidget:
    """Tab content with a launch button that opens the detached popup window."""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setAlignment(Qt.AlignCenter)
    lay.setSpacing(12)

    lbl = QLabel(f"{label}")
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet("color: #546E7A; font-size: 12px;")
    lay.addWidget(lbl)

    btn = QPushButton(f"  ↗  Open {label}")
    btn.setFixedWidth(220)
    btn.setFixedHeight(40)
    btn.setStyleSheet(
        "QPushButton { background: #1565C0; color: white; font-size: 13px;"
        "  font-weight: bold; border-radius: 6px; padding: 4px 12px; }"
        "QPushButton:hover { background: #0D47A1; }"
        "QPushButton:pressed { background: #0A2F6B; }"
    )
    btn.clicked.connect(callback)
    lay.addWidget(btn, alignment=Qt.AlignCenter)
    return w


class _FitPopup(QWidget):
    """
    Floating non-modal window for SMS / QMS raw fit viewers.

    Features
    --------
    • Navigation bar (◀  inlet combo  ▶) — browse inlets without returning
      to the main window.
    • closeEvent re-activates the parser dialog so it comes back to front.
    """

    def __init__(
        self,
        title: str,
        content: QWidget,
        return_to: QWidget,
    ) -> None:
        super().__init__(None, Qt.Window)
        self.setWindowTitle(title)
        self._return_to = return_to
        self._draw_fn = None        # callable(row_idx: int)
        self._current_idx: int = -1

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # ── navigation bar ────────────────────────────────────────────
        nav_row = QHBoxLayout()
        nav_row.setSpacing(4)

        self._btn_prev = QPushButton("◀")
        self._btn_prev.setFixedWidth(32)
        self._btn_prev.setEnabled(False)
        self._btn_prev.setToolTip("Previous inlet")

        self._combo_nav = QComboBox()
        self._combo_nav.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._btn_next = QPushButton("▶")
        self._btn_next.setFixedWidth(32)
        self._btn_next.setEnabled(False)
        self._btn_next.setToolTip("Next inlet")

        nav_row.addWidget(self._btn_prev)
        nav_row.addWidget(self._combo_nav, 1)
        nav_row.addWidget(self._btn_next)
        root.addLayout(nav_row)

        # ── content ───────────────────────────────────────────────────
        root.addWidget(content, 1)

        # ── connections ───────────────────────────────────────────────
        self._btn_prev.clicked.connect(self._go_prev)
        self._btn_next.clicked.connect(self._go_next)
        self._combo_nav.currentIndexChanged.connect(self._on_combo)

        # ── keyboard shortcuts (WidgetWithChildrenShortcut bypasses canvas) ──
        for key in (Qt.Key_Left, Qt.Key_Less, Qt.Key_Comma):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(self._go_prev)
        for key in (Qt.Key_Right, Qt.Key_Greater, Qt.Key_Period):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(self._go_next)

    # ---------------------------------------------------------------- API

    def set_inlets(self, inlets, current_idx: int, draw_fn) -> None:
        """Populate / sync the inlet combo and store the draw callback."""
        self._draw_fn = draw_fn
        self._combo_nav.blockSignals(True)
        if self._combo_nav.count() != len(inlets):
            self._combo_nav.clear()
            for prep in inlets:
                is_sup = getattr(prep, "is_supplemental", False)
                prefix = "↑ " if is_sup else ""
                itype  = getattr(prep, "inlet_type", "")
                label  = f"#{prep.seq_num}  {prefix}{prep.inlet_string or ''}  [{itype}]"
                self._combo_nav.addItem(label)
        self._combo_nav.setCurrentIndex(current_idx)
        self._combo_nav.blockSignals(False)
        self._current_idx = current_idx
        self._sync_buttons()

    # -------------------------------------------------------- internals

    def _go_prev(self) -> None:
        if self._current_idx > 0:
            self._navigate(self._current_idx - 1)

    def _go_next(self) -> None:
        if self._current_idx < self._combo_nav.count() - 1:
            self._navigate(self._current_idx + 1)

    def _on_combo(self, idx: int) -> None:
        if idx >= 0 and idx != self._current_idx:
            self._navigate(idx)

    def _navigate(self, idx: int) -> None:
        self._current_idx = idx
        self._combo_nav.blockSignals(True)
        self._combo_nav.setCurrentIndex(idx)
        self._combo_nav.blockSignals(False)
        self._sync_buttons()
        if self._draw_fn:
            self._draw_fn(idx)

    def _sync_buttons(self) -> None:
        n = self._combo_nav.count()
        self._btn_prev.setEnabled(self._current_idx > 0)
        self._btn_next.setEnabled(self._current_idx < n - 1)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        super().closeEvent(event)
        if self._return_to:
            w = self._return_to.window()
            w.raise_()
            w.activateWindow()


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class NGAMResultsWidget(QWidget):
    """
    Reusable results-display widget.  Call set_data() after processing.

    Signals
    -------
    result_changed(SequenceProcessingResult)
        Emitted when the pipeline is re-run due to a user outlier override.
    outliers_reset(SequenceProcessingResult)
        Emitted when all user overrides are cleared and auto-detection re-runs.
    """

    result_changed  = pyqtSignal(object)
    outliers_reset  = pyqtSignal(object)
    can_reset_changed = pyqtSignal(bool)   # emitted when reset button should be enabled/disabled

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._seq: Optional[ProtocolSequence] = None
        self._result: Optional[SequenceProcessingResult] = None
        self._config: ProcessingConfig = ProcessingConfig()
        # {inlet_seq_num: {device: {action: [bool, ...]}}}
        self._flag_overrides: Dict[int, Dict[str, Dict[str, List[bool]]]] = {}
        self._repro_references: Optional[List] = None
        self._multi_run_linearity: Optional[List] = None
        self._aliquot_volumes: Optional[Dict[int, float]] = None
        # SMS raw-fit panel state (persisted across inlet changes)
        self._sms_loaded_path: Optional[str] = None   # avoid reload for same file
        # User manual fit model overrides: {seq_num: {isotope_key: model_name}}
        self._fit_model_overrides: Dict[int, Dict[str, str]] = {}
        # Standard/blank inlet exclusions: {seq_num: None (all iso) | set of iso_keys}
        self._excluded_standards: Dict[int, Optional[Set[str]]] = {}
        self._excluded_blanks: Dict[int, Optional[Set[str]]] = {}
        # Force-included (un-rejected) standard/blank inlets, same shape
        self._force_included_standards: Dict[int, Optional[Set[str]]] = {}
        self._force_included_blanks: Dict[int, Optional[Set[str]]] = {}
        # Per-species calibration fit-type overrides: {"Device:Isotope": model_name}
        self._blank_fit_overrides: Dict[str, str] = {}
        self._drift_fit_overrides: Dict[str, str] = {}
        self._linearity_fit_overrides: Dict[str, str] = {}
        self._unc_trace: List[str] = []
        # Per-inlet dilution factors: {seq_num: float}.  User-editable via tree.
        self._dilution_factors: Dict[int, float] = {}
        # Detached popup windows for raw fit viewers
        self._sms_popup: Optional[_FitPopup] = None
        self._gauge_inlet_popup: Optional[_FitPopup] = None
        self._qms_popup: Optional[_FitPopup] = None
        self._inlet_sig_popup: Optional[_FitPopup] = None
        self._setup_ui()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def preview_sequence(self, seq: ProtocolSequence) -> None:
        """
        Show parsed inlet names/types immediately after parsing, before
        processing.  Populates the inlet list; clears all results panels.
        """
        self._seq = seq
        self._result = None
        self._flag_overrides = {}
        self._fit_model_overrides = {}
        self._excluded_standards = {}
        self._excluded_blanks = {}
        self._force_included_standards = {}
        self._force_included_blanks = {}
        self._blank_fit_overrides = {}
        self._drift_fit_overrides = {}
        self._linearity_fit_overrides = {}
        self._unc_trace = []
        self._dilution_factors = {}
        self.can_reset_changed.emit(False)
        self._iso_row_w.setVisible(False)

        # Clear results/summary tables
        for tbl in (self._results_tbl, self._final_tbl, self._summary_tbl, self._dissolved_tbl):
            tbl.setRowCount(0)
        for dev_tbl in self._signal_tables.values():
            dev_tbl.setRowCount(0)
        for fig in (self._blank_fig, self._prog_fig,
                    self._qc_fig, self._drift_fig, self._lin_fig,
                    self._srg_fig, self._detail_fig):
            fig.clear()

        # Populate inlet tree from InletPrep objects directly
        self._build_inlet_tree(seq, inlets=seq.inlets, result_inlets=None)

        # Enable parse-only viewers; keep result-dependent ones disabled
        self._set_hub_btn_states(parsed=True, processed=False)

    def clear(self) -> None:
        """Clear all in-memory references and GUI elements."""
        self._seq = None
        self._result = None
        self._flag_overrides = {}
        self._fit_model_overrides = {}
        self._excluded_standards = {}
        self._excluded_blanks = {}
        self._force_included_standards = {}
        self._force_included_blanks = {}
        self._blank_fit_overrides = {}
        self._drift_fit_overrides = {}
        self._linearity_fit_overrides = {}
        self._unc_trace = []
        self._dilution_factors = {}
        self._repro_references = None
        self._multi_run_linearity = None
        self._aliquot_volumes = None
        self._sms_loaded_path = None
        
        # Close any active detached popups
        for popup_attr in ("_sms_popup", "_gauge_inlet_popup", "_qms_popup", "_inlet_sig_popup"):
            popup = getattr(self, popup_attr, None)
            if popup:
                try:
                    popup.close()
                except Exception:
                    pass
                setattr(self, popup_attr, None)

        self.can_reset_changed.emit(False)
        self._iso_row_w.setVisible(False)

        # Clear results/summary tables
        for tbl in (self._results_tbl, self._final_tbl, self._summary_tbl, self._dissolved_tbl, self._gauge_tbl):
            tbl.setRowCount(0)
        for dev_tbl in self._signal_tables.values():
            dev_tbl.setRowCount(0)
        for fig in (self._blank_fig, self._prog_fig,
                    self._qc_fig, self._drift_fig, self._lin_fig,
                    self._srg_fig, self._detail_fig):
            try:
                fig.clear()
            except Exception:
                pass
        self._inlet_tree.clear()
        self._unc_tree.clear()
        self._lbl_unc_empty.setVisible(False)
        self._set_hub_btn_states(parsed=False, processed=False)

    def set_data(
        self,
        seq: ProtocolSequence,
        result: SequenceProcessingResult,
        config: Optional[ProcessingConfig] = None,
        repro_references: Optional[List] = None,
        multi_run_linearity: Optional[List] = None,
        aliquot_volumes: Optional[Dict[int, float]] = None,
        fit_model_overrides: Optional[Dict[int, Dict[str, str]]] = None,
        excluded_standards: Optional[Dict[int, Optional[Set[str]]]] = None,
        excluded_blanks: Optional[Dict[int, Optional[Set[str]]]] = None,
        force_included_standards: Optional[Dict[int, Optional[Set[str]]]] = None,
        force_included_blanks: Optional[Dict[int, Optional[Set[str]]]] = None,
        blank_fit_overrides: Optional[Dict[str, str]] = None,
        drift_fit_overrides: Optional[Dict[str, str]] = None,
        linearity_fit_overrides: Optional[Dict[str, str]] = None,
        unc_trace: Optional[List[str]] = None,
    ) -> None:
        """Load a new sequence result.  Seeds user overrides from the caller
        (e.g. the viewmodel's DB-seeded + session state), rather than always
        resetting to empty, so overrides set before a reprocess survive it."""
        self._seq = seq
        self._result = result
        self._config = config or ProcessingConfig()
        self._flag_overrides = {}
        self._fit_model_overrides = fit_model_overrides or {}
        self._excluded_standards = excluded_standards or {}
        self._excluded_blanks = excluded_blanks or {}
        self._force_included_standards = force_included_standards or {}
        self._force_included_blanks = force_included_blanks or {}
        self._blank_fit_overrides = blank_fit_overrides or {}
        self._drift_fit_overrides = drift_fit_overrides or {}
        self._linearity_fit_overrides = linearity_fit_overrides or {}
        self._unc_trace = unc_trace or []
        self._repro_references = repro_references
        self._multi_run_linearity = multi_run_linearity
        self._aliquot_volumes = aliquot_volumes
        self.can_reset_changed.emit(True)
        self._build_inlet_tree(seq, inlets=seq.inlets, result_inlets=result.inlets)
        self._populate_summary_tab()
        self._populate_qc_tab()
        self._populate_gauge_tab()
        self._populate_sequence_tabs()
        self._populate_unc_prop_tab()
        self._iso_row_w.setVisible(True)
        self._select_first_leaf()

        # All four inlet viewers now have the data they need
        self._set_hub_btn_states(parsed=True, processed=True)

    def get_result(self) -> Optional[SequenceProcessingResult]:
        return self._result

    def reset_outliers(self) -> None:
        """Public slot: discard all manual overrides and re-run auto-detection."""
        self._reset_outliers()

    def add_inlet_list_action(self, widget: QWidget) -> None:
        """Add a widget below the inlet tree (e.g. Supplemental Runs button)."""
        self._left_lay.addWidget(widget)

    # ------------------------------------------------------------------
    # Internal helpers: tree navigation
    # ------------------------------------------------------------------

    def _current_inlet_idx(self) -> int:
        """Return the index into self._seq.inlets for the selected tree leaf, or -1."""
        items = self._inlet_tree.selectedItems()
        if not items:
            return -1
        item = items[0]
        if item.parent() is None:   # group header selected, not a leaf
            return -1
        idx = item.data(0, Qt.UserRole)
        return idx if idx is not None else -1

    def _select_inlet_by_idx(self, idx: int) -> None:
        """Select the tree leaf whose UserRole equals idx."""
        root = self._inlet_tree.invisibleRootItem()
        for g in range(root.childCount()):
            group = root.child(g)
            for i in range(group.childCount()):
                leaf = group.child(i)
                if leaf.data(0, Qt.UserRole) == idx:
                    self._inlet_tree.setCurrentItem(leaf)
                    return

    def _select_first_leaf(self) -> None:
        """Select the very first inlet leaf in the tree."""
        root = self._inlet_tree.invisibleRootItem()
        for g in range(root.childCount()):
            group = root.child(g)
            if group.childCount() > 0:
                self._inlet_tree.setCurrentItem(group.child(0))
                return

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        h_split = QSplitter(Qt.Horizontal)
        h_split.setChildrenCollapsible(False)

        # ── Left: inlet list ─────────────────────────────────────────
        left_w = QWidget()
        left_lay = QVBoxLayout(left_w)
        left_lay.setContentsMargins(4, 4, 4, 4)
        left_lay.setSpacing(4)

        _inlet_headers = ["#", "Name", "Type", "Df", "Ref (ccSTP)"]
        self._inlet_tree = QTreeWidget()
        self._inlet_tree.setStyleSheet(_TABLE_SS)
        self._inlet_tree.setAlternatingRowColors(False)
        self._inlet_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._inlet_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._inlet_tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._inlet_tree.setColumnCount(len(_inlet_headers))
        self._inlet_tree.setHeaderLabels(_inlet_headers)
        self._inlet_tree.header().setStyleSheet(_HDR_SS)
        self._inlet_tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (0, 2, 3, 4):
            self._inlet_tree.header().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self._inlet_tree.header().setDefaultAlignment(Qt.AlignCenter)
        self._inlet_tree.setRootIsDecorated(True)
        self._inlet_tree.setItemsExpandable(True)
        self._inlet_tree.setUniformRowHeights(False)
        self._inlet_tree.itemSelectionChanged.connect(self._on_inlet_selected)
        self._inlet_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._inlet_tree.customContextMenuRequested.connect(self._on_inlet_tree_context_menu)
        left_lay.addWidget(self._inlet_tree, 1)

        self._left_lay = left_lay   # for add_inlet_list_action()

        h_split.addWidget(left_w)

        # ── Right: chart tabs (top) + data tabs (bottom) ──────────────
        right_w = QWidget()
        right_lay = QVBoxLayout(right_w)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(0)

        v_split = QSplitter(Qt.Vertical)
        v_split.setChildrenCollapsible(False)

        # ── TOP: chart tabs container ─────────────────────────────────
        chart_top_w = QWidget()
        chart_top_lay = QVBoxLayout(chart_top_w)
        chart_top_lay.setContentsMargins(0, 0, 0, 0)
        chart_top_lay.setSpacing(0)

        # Isotope selector — shown for all sequence-level chart tabs
        self._iso_row_w = QWidget()
        iso_lay = QHBoxLayout(self._iso_row_w)
        iso_lay.setContentsMargins(6, 3, 6, 2)
        iso_lay.setSpacing(4)
        iso_lay.addWidget(QLabel("Isotope:"))
        self._btn_iso_prev = QPushButton("◀")
        self._btn_iso_prev.setFixedWidth(26)
        self._btn_iso_prev.setToolTip("Previous isotope")
        self._btn_iso_prev.clicked.connect(self._iso_prev)
        iso_lay.addWidget(self._btn_iso_prev)
        self._combo_isotope = QComboBox()
        self._combo_isotope.setMinimumWidth(150)
        self._combo_isotope.currentIndexChanged.connect(self._on_isotope_changed)
        iso_lay.addWidget(self._combo_isotope)
        self._btn_iso_next = QPushButton("▶")
        self._btn_iso_next.setFixedWidth(26)
        self._btn_iso_next.setToolTip("Next isotope")
        self._btn_iso_next.clicked.connect(self._iso_next)
        iso_lay.addWidget(self._btn_iso_next)

        # Per-isotope calibration fit-type overrides -- distinct from the
        # sequence-wide Blank/Drift/Linearity combos in the toolbar above
        # (which set the default for every isotope): these override just the
        # currently-selected isotope's fit, same precedence as web's
        # blankFitOverrides/driftFitOverrides/linearityFitOverrides.
        iso_lay.addSpacing(12)
        iso_lay.addWidget(QLabel("Blank ovr:"))
        self._combo_blank_override = QComboBox()
        self._combo_blank_override.addItem("(default)", None)
        for label, val in [("Auto (AICc)", "auto"), ("Mean", "mean"), ("Akima", "akima"),
                            ("Linear", "linear"), ("Quadratic", "quadratic"), ("Cubic", "cubic")]:
            self._combo_blank_override.addItem(label, val)
        self._combo_blank_override.setFixedWidth(100)
        self._combo_blank_override.currentIndexChanged.connect(
            lambda: self._on_fit_override_changed(self._blank_fit_overrides, self._combo_blank_override)
        )
        iso_lay.addWidget(self._combo_blank_override)

        iso_lay.addWidget(QLabel("Drift ovr:"))
        self._combo_drift_override = QComboBox()
        self._combo_drift_override.addItem("(default)", None)
        for label, val in [("Auto (AICc)", "auto"), ("None", "none"), ("Akima", "akima"),
                            ("Linear", "linear"), ("Quadratic", "quadratic"), ("Cubic", "cubic"),
                            ("Exponential", "exponential")]:
            self._combo_drift_override.addItem(label, val)
        self._combo_drift_override.setFixedWidth(100)
        self._combo_drift_override.currentIndexChanged.connect(
            lambda: self._on_fit_override_changed(self._drift_fit_overrides, self._combo_drift_override)
        )
        iso_lay.addWidget(self._combo_drift_override)

        iso_lay.addWidget(QLabel("Lin ovr:"))
        self._combo_linearity_override = QComboBox()
        self._combo_linearity_override.addItem("(default)", None)
        for label, val in [("Auto (AICc)", "auto"), ("None", "none"),
                            ("Linear", "linear"), ("Quadratic", "quadratic")]:
            self._combo_linearity_override.addItem(label, val)
        self._combo_linearity_override.setFixedWidth(100)
        self._combo_linearity_override.currentIndexChanged.connect(
            lambda: self._on_fit_override_changed(self._linearity_fit_overrides, self._combo_linearity_override)
        )
        iso_lay.addWidget(self._combo_linearity_override)

        iso_lay.addStretch(1)
        self._iso_row_w.setVisible(False)
        chart_top_lay.addWidget(self._iso_row_w)

        # Arrow-key shortcuts for isotope navigation — only active when the
        # isotope row is visible (calibration chart tabs); _iso_prev/_iso_next
        # guard against firing on Inlet Plots / hidden state.
        for _key in (Qt.Key_Left, Qt.Key_Less, Qt.Key_Comma):
            _sc = QShortcut(QKeySequence(_key), self)
            _sc.setContext(Qt.WidgetWithChildrenShortcut)
            _sc.activated.connect(self._iso_prev)
        for _key in (Qt.Key_Right, Qt.Key_Greater, Qt.Key_Period):
            _sc = QShortcut(QKeySequence(_key), self)
            _sc.setContext(Qt.WidgetWithChildrenShortcut)
            _sc.activated.connect(self._iso_next)

        self._chart_tabs = QTabWidget()

        # ── Inlet Plots hub — all per-inlet popup viewers in one place ────
        # Initialise the underlying widgets (they live in their popups, not tabs)
        self._inlet_signals_widget = InletSignalsWidget()

        self._sms_panel_4he = SMSFitPanel()
        self._sms_panel_3he = SMSFitPanel()
        self._sms_panel_4he.model_selected.connect(
            lambda m: self._on_sms_model_selected(m, "4He")
        )
        self._sms_panel_3he.model_selected.connect(
            lambda m: self._on_sms_model_selected(m, "3He")
        )

        self._qms_sub_tabs = QTabWidget()
        self._qms_panel_ar   = QMSFitWidget()
        self._qms_panel_ne   = QMSFitWidget()
        self._qms_panel_krxe = QMSFitWidget()

        def _connect_qms_panel(panel: QMSFitWidget, device: str) -> None:
            panel.model_changed.connect(
                lambda iso, model, dev=device:
                    self._on_qms_model_changed(dev, iso, model)
            )

        _connect_qms_panel(self._qms_panel_ar,   "QMSAr")
        _connect_qms_panel(self._qms_panel_ne,   "QMSNe")
        _connect_qms_panel(self._qms_panel_krxe, "QMSKrXe")

        self._qms_sub_tabs.addTab(self._qms_panel_ar,   "Ar")
        self._qms_sub_tabs.addTab(self._qms_panel_ne,   "Ne")
        self._qms_sub_tabs.addTab(self._qms_panel_krxe, "KrXe")

        self._detail_fig = Figure(figsize=(8, 4), tight_layout=True)
        self._detail_canvas = FigureCanvas(self._detail_fig)
        self._detail_canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._chart_tabs.addTab(self._build_inlet_plots_hub(), "Inlet Plots")

        # ── Sequence-level calibration chart canvases (pipeline order) ───
        self._blank_fig = Figure(figsize=(6, 3), tight_layout=True)
        self._blank_canvas = FigureCanvas(self._blank_fig)
        self._blank_canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._chart_tabs.addTab(self._blank_canvas, "Blank Fit")

        self._drift_fig = Figure(figsize=(6, 3), constrained_layout=True)
        self._drift_canvas = FigureCanvas(self._drift_fig)
        self._drift_canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._chart_tabs.addTab(self._drift_canvas, "Drift Corr.")

        self._lin_fig = Figure(figsize=(6, 3), constrained_layout=True)
        self._lin_canvas = FigureCanvas(self._lin_fig)
        self._lin_canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._chart_tabs.addTab(self._lin_canvas, "Linearity")

        self._prog_fig = Figure(figsize=(6, 3), tight_layout=True)
        self._prog_canvas = FigureCanvas(self._prog_fig)
        self._prog_canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._chart_tabs.addTab(self._prog_canvas, "Signal Prog.")

        self._qc_fig = Figure(figsize=(6, 3), tight_layout=True)
        self._qc_canvas = FigureCanvas(self._qc_fig)
        self._qc_canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        _qc_tab_w = QWidget()
        _qc_tab_lay = QVBoxLayout(_qc_tab_w)
        _qc_tab_lay.setContentsMargins(0, 0, 0, 0)
        _qc_tab_lay.setSpacing(0)
        _qc_toolbar = QHBoxLayout()
        _qc_toolbar.setContentsMargins(8, 4, 8, 4)
        _qc_toolbar.addWidget(QLabel("View:"))
        self._combo_qc_mode = QComboBox()
        self._combo_qc_mode.addItem("Standard Reproducibility", "repro")
        self._combo_qc_mode.addItem("Blank-Corrected Signal vs ccSTP", "blank_vs_cc")
        self._combo_qc_mode.addItem("Drift+Linearity Signal vs ccSTP", "drift_lin_vs_cc")
        self._combo_qc_mode.currentIndexChanged.connect(self._draw_qc_chart)
        _qc_toolbar.addWidget(self._combo_qc_mode)
        _qc_toolbar.addStretch()
        _qc_tab_lay.addLayout(_qc_toolbar)
        _qc_tab_lay.addWidget(self._qc_canvas)
        self._chart_tabs.addTab(_qc_tab_w, "QC Chart")

        # Gauge Signal — full-sequence SRG / Total Pressure time-series + fits
        self._srg_fig = Figure(figsize=(8, 3), tight_layout=True)
        self._srg_canvas = FigureCanvas(self._srg_fig)
        self._srg_canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._chart_tabs.addTab(self._srg_canvas, "Gauge Signal")

        self._chart_tabs.currentChanged.connect(self._on_chart_tab_changed)
        chart_top_lay.addWidget(self._chart_tabs, 1)
        v_split.addWidget(chart_top_w)

        # ── BOTTOM: data tabs ─────────────────────────────────────────
        self._data_tabs = QTabWidget()

        # Signals tab — per-device interactive signal rows
        self._device_tabs = QTabWidget()
        self._signal_tables: Dict[str, QTableWidget] = {}
        _sig_headers = ["#", "Action", "Time (s)", "Signal (A)", "Detector", "Outlier"]
        for dev in _DEVICES:
            tbl = QTableWidget()
            tbl.setStyleSheet(_TABLE_SS)
            tbl.setAlternatingRowColors(True)
            tbl.setShowGrid(False)
            tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
            tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
            tbl.setColumnCount(len(_sig_headers))
            tbl.setHorizontalHeaderLabels(_sig_headers)
            tbl.horizontalHeader().setStyleSheet(_HDR_SS)
            tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            for _c in (0, 2, 3, 4, 5):
                tbl.horizontalHeader().setSectionResizeMode(_c, QHeaderView.ResizeToContents)
            tbl.verticalHeader().setVisible(False)
            tbl.itemChanged.connect(self._on_signal_item_changed)
            self._signal_tables[dev] = tbl
            self._device_tabs.addTab(tbl, dev)

        self._device_tabs.currentChanged.connect(self._on_device_tab_changed)
        self._data_tabs.addTab(self._device_tabs, "Signals")

        # Results tab — per-inlet isotope breakdown
        _res_headers = [
            "Device", "Isotope", "Fit", "N used",
            "Meas (A)", "Block BG (A)", "Net (A)",
            "Seq. Blank (A)", "BC (A)", "Ref (ccSTP)", "ccSTP", "± ccSTP", "BG Used",
        ]
        self._results_tbl = QTableWidget()
        self._results_tbl.setStyleSheet(_TABLE_SS)
        self._results_tbl.setAlternatingRowColors(True)
        self._results_tbl.setShowGrid(False)
        self._results_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._results_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._results_tbl.setColumnCount(len(_res_headers))
        self._results_tbl.setHorizontalHeaderLabels(_res_headers)
        self._results_tbl.horizontalHeader().setStyleSheet(_HDR_SS)
        self._results_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for _c in (0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12):
            self._results_tbl.horizontalHeader().setSectionResizeMode(
                _c, QHeaderView.ResizeToContents
            )
        self._results_tbl.verticalHeader().setVisible(False)
        self._data_tabs.addTab(self._results_tbl, "Results")

        # Final Results tab — sample ccSTP summary
        _final_headers = [
            "#", "Inlet", "Type",
            "Device", "Isotope",
            "ccSTP (mean S)", "± (mean S)",
            "ccSTP (drift S)", "± (drift S)",
        ]
        self._final_tbl = QTableWidget()
        self._final_tbl.setStyleSheet(_TABLE_SS)
        self._final_tbl.setAlternatingRowColors(True)
        self._final_tbl.setShowGrid(False)
        self._final_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._final_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._final_tbl.setColumnCount(len(_final_headers))
        self._final_tbl.setHorizontalHeaderLabels(_final_headers)
        self._final_tbl.horizontalHeader().setStyleSheet(_HDR_SS)
        self._final_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for _c in (0, 2, 3, 4, 5, 6, 7, 8):
            self._final_tbl.horizontalHeader().setSectionResizeMode(
                _c, QHeaderView.ResizeToContents
            )
        self._final_tbl.verticalHeader().setVisible(False)
        self._data_tabs.addTab(self._final_tbl, "Final Results")

        # Dissolved Conc. tab — water extraction results (only populated when extraction_info present)
        _diss_headers = [
            "#", "Inlet", "Device", "Isotope",
            "η", "ccSTP_true", "± true",
            "ccSTP/g", "± /g",
            "C_eq (cm³/g)", "meas/eq",
        ]
        self._dissolved_tbl = QTableWidget()
        self._dissolved_tbl.setStyleSheet(_TABLE_SS)
        self._dissolved_tbl.setAlternatingRowColors(True)
        self._dissolved_tbl.setShowGrid(False)
        self._dissolved_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._dissolved_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._dissolved_tbl.setColumnCount(len(_diss_headers))
        self._dissolved_tbl.setHorizontalHeaderLabels(_diss_headers)
        self._dissolved_tbl.horizontalHeader().setStyleSheet(_HDR_SS)
        self._dissolved_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for _c in (0, 2, 3, 4, 5, 6, 7, 8, 9, 10):
            self._dissolved_tbl.horizontalHeader().setSectionResizeMode(
                _c, QHeaderView.ResizeToContents
            )
        self._dissolved_tbl.verticalHeader().setVisible(False)
        self._data_tabs.addTab(self._dissolved_tbl, "Dissolved Conc.")

        # Within-inlet Ratios tab — one ratio per column, per-inlet rows
        self._ratios_tbl = QTableWidget()
        self._ratios_tbl.setStyleSheet(_TABLE_SS)
        self._ratios_tbl.setAlternatingRowColors(True)
        self._ratios_tbl.setShowGrid(False)
        self._ratios_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._ratios_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._ratios_tbl.verticalHeader().setVisible(False)
        self._data_tabs.addTab(self._ratios_tbl, "Ratios")

        # Cross-inlet Ratios tab — only populated when extraction info present
        self._cross_ratios_tbl = QTableWidget()
        self._cross_ratios_tbl.setStyleSheet(_TABLE_SS)
        self._cross_ratios_tbl.setAlternatingRowColors(True)
        self._cross_ratios_tbl.setShowGrid(False)
        self._cross_ratios_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._cross_ratios_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._cross_ratios_tbl.verticalHeader().setVisible(False)
        self._data_tabs.addTab(self._cross_ratios_tbl, "Cross Ratios")

        # Summary tab — blank means and sensitivities
        _sum_headers = [
            "Device", "Isotope",
            "Seq. Blank (A)", "± Blank",
            "Sensitivity (A/ccSTP)", "± Sens",
        ]
        self._summary_tbl = QTableWidget()
        self._summary_tbl.setStyleSheet(_TABLE_SS)
        self._summary_tbl.setAlternatingRowColors(True)
        self._summary_tbl.setShowGrid(False)
        self._summary_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._summary_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._summary_tbl.setColumnCount(len(_sum_headers))
        self._summary_tbl.setHorizontalHeaderLabels(_sum_headers)
        self._summary_tbl.horizontalHeader().setStyleSheet(_HDR_SS)
        self._summary_tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._summary_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for _c in (2, 3, 4, 5):
            self._summary_tbl.horizontalHeader().setSectionResizeMode(
                _c, QHeaderView.ResizeToContents
            )
        self._summary_tbl.verticalHeader().setVisible(False)
        self._data_tabs.addTab(self._summary_tbl, "Summary")

        # QC tab — calibration fit summary
        self._qc_browser = QTextBrowser()
        self._qc_browser.setOpenExternalLinks(False)
        self._qc_browser.setStyleSheet("font-size: 11px;")
        self._data_tabs.addTab(self._qc_browser, "QC")

        # Gauge tab (bottom) — Summary table only
        _gauge_hdr = ["#", "Inlet", "Type"] + PRIMARY_CHANNELS + SECONDARY_CHANNELS
        self._gauge_tbl = QTableWidget()
        self._gauge_tbl.setStyleSheet(_TABLE_SS)
        self._gauge_tbl.setAlternatingRowColors(True)
        self._gauge_tbl.setShowGrid(False)
        self._gauge_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._gauge_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._gauge_tbl.setColumnCount(len(_gauge_hdr))
        self._gauge_tbl.setHorizontalHeaderLabels(_gauge_hdr)
        self._gauge_tbl.horizontalHeader().setStyleSheet(_HDR_SS)
        self._gauge_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for _c in range(len(_gauge_hdr)):
            if _c != 1:
                self._gauge_tbl.horizontalHeader().setSectionResizeMode(
                    _c, QHeaderView.ResizeToContents
                )
        self._gauge_tbl.verticalHeader().setVisible(False)

        _gauge_tab_w = QWidget()
        _gauge_tab_lay = QVBoxLayout(_gauge_tab_w)
        _gauge_tab_lay.setContentsMargins(0, 0, 0, 0)
        _gauge_tab_lay.setSpacing(0)
        _gauge_toolbar = QHBoxLayout()
        _gauge_toolbar.setContentsMargins(8, 4, 8, 4)
        self._chk_gauge_samples_only = QCheckBox("Samples only")
        self._chk_gauge_samples_only.toggled.connect(lambda _: self._populate_gauge_tab())
        _gauge_toolbar.addWidget(self._chk_gauge_samples_only)
        self._lbl_gauge_filter_count = QLabel("")
        self._lbl_gauge_filter_count.setStyleSheet("color:#78909c; font-size:11px;")
        _gauge_toolbar.addWidget(self._lbl_gauge_filter_count)
        _gauge_toolbar.addStretch()
        _gauge_tab_lay.addLayout(_gauge_toolbar)
        _gauge_tab_lay.addWidget(self._gauge_tbl)
        self._data_tabs.addTab(_gauge_tab_w, "Gauge")

        # Unc. Prop. tab — per-inlet uncertainty-propagation trace with jump detection
        _unc_tab_w = QWidget()
        _unc_tab_lay = QVBoxLayout(_unc_tab_w)
        _unc_tab_lay.setContentsMargins(0, 0, 0, 0)
        _unc_tab_lay.setSpacing(0)
        _unc_toolbar = QHBoxLayout()
        _unc_toolbar.setContentsMargins(8, 4, 8, 4)
        _unc_toolbar.addWidget(QLabel("Isotope:"))
        self._combo_unc_isotope = QComboBox()
        self._combo_unc_isotope.addItem("All", None)
        self._combo_unc_isotope.currentIndexChanged.connect(lambda _: self._populate_unc_prop_tab())
        _unc_toolbar.addWidget(self._combo_unc_isotope)
        _unc_toolbar.addStretch()
        _unc_tab_lay.addLayout(_unc_toolbar)

        self._unc_tree = QTreeWidget()
        self._unc_tree.setColumnCount(6)
        self._unc_tree.setHeaderLabels(["Stage", "Isotope", "Value", "± Unc", "Rel %", "Jump"])
        self._unc_tree.setAlternatingRowColors(True)
        self._unc_tree.setStyleSheet(_TABLE_SS)
        self._unc_tree.header().setStyleSheet(_HDR_SS)
        self._unc_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._unc_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        for c in (2, 3, 4, 5):
            self._unc_tree.header().setSectionResizeMode(c, QHeaderView.Stretch)
        _unc_tab_lay.addWidget(self._unc_tree)
        self._lbl_unc_empty = QLabel(
            "No uncertainty trace available for this run yet. "
            "It's generated automatically the next time you Process Data."
        )
        self._lbl_unc_empty.setStyleSheet("color:#78909c; padding:16px;")
        self._lbl_unc_empty.setAlignment(Qt.AlignCenter)
        self._lbl_unc_empty.setVisible(False)
        _unc_tab_lay.addWidget(self._lbl_unc_empty)
        self._data_tabs.addTab(_unc_tab_w, "Unc. Prop.")

        v_split.addWidget(self._data_tabs)
        v_split.setSizes([380, 220])

        right_lay.addWidget(v_split, 1)
        h_split.addWidget(right_w)
        h_split.setSizes([280, 720])

        root.addWidget(h_split)

    def _build_inlet_plots_hub(self) -> QWidget:
        """
        First tab of the top panel: a grid of launch buttons, one per
        inlet-level popup viewer.  Users can open any combination simultaneously.
        Buttons are disabled until data is available (parse → process).
        """
        _CARD_SS = (
            "QPushButton {"
            "  background: #1565C0; color: white;"
            "  font-size: 13px; font-weight: bold;"
            "  border-radius: 8px; padding: 14px 20px;"
            "  text-align: left; }"
            "QPushButton:hover   { background: #0D47A1; }"
            "QPushButton:pressed { background: #0A2F6B; }"
            "QPushButton:disabled { background: #90A4AE; color: #CFD8DC; }"
        )

        # (label, description, callback, needs_result)
        # needs_result=True  → enable only after process_sequence()
        # needs_result=False → enable after parsing
        viewers = [
            (
                "↗  Inlet Signals",
                "Per-action raw signal timelines for the selected inlet",
                self._draw_inlet_signals,
                True,   # requires processed InletResult
            ),
            (
                "↗  SMS Raw Fit",
                "⁴He / ³He sector-field fit panels (Average · Linear · Poly · Exp)",
                self._draw_sms_raw_fit,
                False,  # works from parsed sequence alone
            ),
            (
                "↗  QMS Raw Fit",
                "Quadrupole fit panels — Ar · Ne · KrXe isotope ratios",
                self._draw_qms_raw_fit,
                False,  # works from parsed sequence alone
            ),
            (
                "↗  Gauge Inlet",
                "SRG / pressure detail scatter for the selected inlet",
                self._draw_gauge_inlet,
                True,   # requires processed gauge summary
            ),
        ]

        hub = QWidget()
        outer = QVBoxLayout(hub)
        outer.setContentsMargins(28, 20, 28, 20)
        outer.setSpacing(14)

        title = QLabel("Inlet Plot Viewers")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #37474F; padding-bottom: 2px;"
        )
        outer.addWidget(title)

        hint = QLabel(
            "Select an inlet in the left panel, then open one or more viewers below.\n"
            "Each viewer is an independent floating window — open as many as your screen allows."
        )
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("font-size: 11px; color: #78909C; padding-bottom: 6px;")
        outer.addWidget(hint)

        # Store (button, needs_result) for later enable/disable control
        self._hub_btns: list = []   # [(QPushButton, needs_result)]

        grid = QGridLayout()
        grid.setSpacing(14)
        for i, (label, desc, callback, needs_result) in enumerate(viewers):
            card = QWidget()
            card_lay = QVBoxLayout(card)
            card_lay.setContentsMargins(0, 0, 0, 0)
            card_lay.setSpacing(4)

            btn = QPushButton(label)
            btn.setStyleSheet(_CARD_SS)
            btn.setFixedHeight(56)
            btn.setEnabled(False)
            btn.clicked.connect(callback)
            card_lay.addWidget(btn)

            desc_lbl = QLabel(desc)
            desc_lbl.setAlignment(Qt.AlignCenter)
            desc_lbl.setStyleSheet("font-size: 10px; color: #90A4AE;")
            card_lay.addWidget(desc_lbl)

            grid.addWidget(card, i // 2, i % 2)
            self._hub_btns.append((btn, needs_result))

        outer.addLayout(grid)
        outer.addStretch(1)
        return hub

    def _set_hub_btn_states(self, parsed: bool, processed: bool) -> None:
        """Update Inlet Plots hub button enabled states."""
        for btn, needs_result in self._hub_btns:
            btn.setEnabled(processed if needs_result else parsed)

    # ------------------------------------------------------------------
    # Outer tab handler
    # ------------------------------------------------------------------

    _SEQ_CHART_TABS = frozenset({
        "Blank Fit", "Signal Prog.", "QC Chart",
        "Drift Corr.", "Linearity",
    })

    def _on_sms_model_selected(self, model: str, isotope: str) -> None:
        if self._seq is None:
            return
        row_idx = self._current_inlet_idx()
        if row_idx < 0 or row_idx >= len(self._seq.inlets):
            return
        prep = self._seq.inlets[row_idx]
        seq_num = prep.seq_num
        if seq_num not in self._fit_model_overrides:
            self._fit_model_overrides[seq_num] = {}
        self._fit_model_overrides[seq_num][f"SMS:{isotope}"] = model

    def _on_qms_model_changed(self, device: str, isotope: str, model: str) -> None:
        if self._seq is None:
            return
        row_idx = self._current_inlet_idx()
        if row_idx < 0 or row_idx >= len(self._seq.inlets):
            return
        prep = self._seq.inlets[row_idx]
        seq_num = prep.seq_num
        if seq_num not in self._fit_model_overrides:
            self._fit_model_overrides[seq_num] = {}
        self._fit_model_overrides[seq_num][f"{device}:{isotope}"] = model

    def _on_chart_tab_changed(self, index: int) -> None:
        tab_text = self._chart_tabs.tabText(index)
        self._dispatch_seq_chart(tab_text)

    def _iso_prev(self) -> None:
        if not self._iso_row_w.isVisible():
            return
        n = self._combo_isotope.count()
        if n > 1:
            self._combo_isotope.setCurrentIndex(
                (self._combo_isotope.currentIndex() - 1) % n
            )

    def _iso_next(self) -> None:
        if not self._iso_row_w.isVisible():
            return
        n = self._combo_isotope.count()
        if n > 1:
            self._combo_isotope.setCurrentIndex(
                (self._combo_isotope.currentIndex() + 1) % n
            )

    def _on_isotope_changed(self) -> None:
        tab_text = self._chart_tabs.tabText(self._chart_tabs.currentIndex())
        self._dispatch_seq_chart(tab_text)
        self._apply_isotope_filter()
        self._sync_fit_override_combos()

    def _sync_fit_override_combos(self) -> None:
        """Reflect the currently-selected isotope's existing per-species
        fit overrides in the 3 override combos, without re-triggering a
        reprocess (blockSignals guard)."""
        key = self._current_iso_key()
        for combo, overrides in [
            (self._combo_blank_override, self._blank_fit_overrides),
            (self._combo_drift_override, self._drift_fit_overrides),
            (self._combo_linearity_override, self._linearity_fit_overrides),
        ]:
            combo.blockSignals(True)
            try:
                val = overrides.get(key) if key else None
                idx = combo.findData(val)
                combo.setCurrentIndex(idx if idx >= 0 else 0)
            finally:
                combo.blockSignals(False)

    def _on_fit_override_changed(self, overrides: Dict[str, str], combo: QComboBox) -> None:
        key = self._current_iso_key()
        if not key:
            return
        val = combo.currentData()
        if val is None:
            overrides.pop(key, None)
        else:
            overrides[key] = val
        self._rerun_pipeline()

    def _apply_isotope_filter(self) -> None:
        """Show/hide rows in Signals, Results, and Final Results tables based on the selected isotope."""
        key = self._combo_isotope.currentData()   # None = "All isotopes"

        # -- Results tab (Device=col 0, Isotope=col 1) --
        tbl = self._results_tbl
        for r in range(tbl.rowCount()):
            dev_item = tbl.item(r, 0)
            iso_item = tbl.item(r, 1)
            if dev_item is None or iso_item is None:
                continue
            row_key = f"{dev_item.text()}:{iso_item.text()}"
            tbl.setRowHidden(r, key is not None and row_key != key)

        # -- Final Results tab (Device=col 3, Isotope=col 4) --
        tbl = self._final_tbl
        for r in range(tbl.rowCount()):
            dev_item = tbl.item(r, 3)
            iso_item = tbl.item(r, 4)
            if dev_item is None or iso_item is None:
                continue
            row_key = f"{dev_item.text()}:{iso_item.text()}"
            tbl.setRowHidden(r, key is not None and row_key != key)

        # -- Signals tab (Action=col 1, per device tab) --
        if key is None:
            for tbl in self._signal_tables.values():
                for r in range(tbl.rowCount()):
                    tbl.setRowHidden(r, False)
        else:
            sel_dev, sel_iso = key.split(":", 1)
            iso_lower = sel_iso.lower()
            for dev, tbl in self._signal_tables.items():
                if dev != sel_dev:
                    for r in range(tbl.rowCount()):
                        tbl.setRowHidden(r, False)
                else:
                    for r in range(tbl.rowCount()):
                        act_item = tbl.item(r, 1)
                        hidden = act_item is None or iso_lower not in act_item.text().lower()
                        tbl.setRowHidden(r, hidden)

    def _dispatch_seq_chart(self, tab_text: str) -> None:
        if tab_text == "Blank Fit":
            self._draw_blank_fit()
        elif tab_text == "Signal Prog.":
            self._draw_signal_progression()
        elif tab_text == "QC Chart":
            self._draw_qc_chart()
        elif tab_text == "Drift Corr.":
            self._draw_drift_correction()
        elif tab_text == "Linearity":
            self._draw_linearity()
        elif tab_text == "Inlet Signals":
            self._draw_inlet_signals()
        elif tab_text == "SMS Raw Fit":
            self._draw_sms_raw_fit()
        elif tab_text == "QMS Raw Fit":
            self._draw_qms_raw_fit()
        # "Gauge Signal" is populated once by _populate_gauge_tab()
        # "Inlet Plots" tab shows the hub widget — no chart to dispatch

    # ------------------------------------------------------------------
    # Sequence-level tabs
    # ------------------------------------------------------------------

    def _populate_sequence_tabs(self) -> None:
        self._populate_final_results()
        self._populate_dissolved_tab()
        self._populate_ratios_tab()
        self._populate_cross_ratios_tab()
        # Rebuild isotope combo from current result
        self._combo_isotope.blockSignals(True)
        prev_key = self._combo_isotope.currentData()
        self._combo_isotope.clear()
        self._combo_isotope.addItem("All isotopes", None)
        if self._result is not None:
            all_keys = sorted(
                {k for ir in self._result.inlets for k in ir.isotopes}
            )
            for key in all_keys:
                self._combo_isotope.addItem(key, key)
            # Restore previous selection if still present
            idx = self._combo_isotope.findData(prev_key)
            if idx >= 0:
                self._combo_isotope.setCurrentIndex(idx)
        self._combo_isotope.blockSignals(False)

        # Apply filter to Final Results table (already populated above)
        self._apply_isotope_filter()

        # Lazily draw whichever chart tab is currently visible
        tab_text = self._chart_tabs.tabText(self._chart_tabs.currentIndex())
        if tab_text in self._SEQ_CHART_TABS:
            self._dispatch_seq_chart(tab_text)
        else:
            # Clear sequence charts so they refresh on next visit
            for fig in (self._blank_fig, self._prog_fig,
                        self._qc_fig, self._drift_fig, self._lin_fig):
                fig.clear()

    def _populate_final_results(self) -> None:
        tbl = self._final_tbl
        tbl.setRowCount(0)
        if self._result is None:
            return

        def _fmt(v: float) -> str:
            return f"{v:.4e}" if not math.isnan(v) else "—"

        prep_by_seq = (
            {p.seq_num: p for p in self._seq.inlets} if self._seq else {}
        )
        rows = []
        for ir in self._result.inlets:
            prep = prep_by_seq.get(ir.seq_num)
            if getattr(prep, "is_supplemental", False):
                continue   # supplemental inlets are calibration only
            for key in sorted(ir.isotopes.keys()):
                iso = ir.isotopes[key]
                device, isotope = key.split(":", 1)
                if not _is_reportable_action(isotope):
                    continue   # skip PeakCenter, PeakRaw, PumpDown, Scan, Inlet phases
                rows.append((
                    ir.seq_num, ir.inlet_string, ir.inlet_type,
                    device, isotope,
                    iso.ccSTP, iso.ccSTP_unc,
                    iso.drift_ccSTP, iso.drift_ccSTP_unc,
                ))

        tbl.setRowCount(len(rows))
        for r, (sn, name, itype, dev, iso, ccstp, ccstp_u, dccstp, dccstp_u) in enumerate(rows):
            bg = _TYPE_BG.get(itype, QColor("#FFFFFF"))
            cells = [
                QTableWidgetItem(str(sn)),
                QTableWidgetItem(name),
                QTableWidgetItem(itype.capitalize()),
                QTableWidgetItem(dev),
                QTableWidgetItem(iso),
                QTableWidgetItem(_fmt(ccstp)),
                QTableWidgetItem(_fmt(ccstp_u)),
                QTableWidgetItem(_fmt(dccstp)),
                QTableWidgetItem(_fmt(dccstp_u)),
            ]
            for c, it in enumerate(cells):
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                it.setBackground(bg)
                if c in (0, 2, 3, 4):
                    it.setTextAlignment(Qt.AlignCenter)
                elif c >= 5:
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                else:
                    it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                tbl.setItem(r, c, it)

    def _populate_dissolved_tab(self) -> None:
        """Populate the Dissolved Conc. tab with water-extraction corrected results.

        Only rows where ccSTP_per_g is not NaN are shown, so the tab is
        effectively empty unless Step 11 ran (i.e. extraction_info was provided).
        """
        tbl = self._dissolved_tbl
        tbl.setRowCount(0)
        if self._result is None:
            return

        def _fmt(v: float, digits: int = 4) -> str:
            return f"{v:.{digits}e}" if not math.isnan(v) else "—"

        prep_by_seq = {p.seq_num: p for p in self._seq.inlets} if self._seq else {}
        rows = []
        for ir in self._result.inlets:
            if ir.inlet_type != "sample":
                continue
            prep = prep_by_seq.get(ir.seq_num)
            if getattr(prep, "is_supplemental", False):
                continue
            for key in sorted(ir.isotopes.keys()):
                iso = ir.isotopes[key]
                if math.isnan(iso.ccSTP_per_g):
                    continue
                device, isotope = key.split(":", 1)
                if not _is_reportable_action(isotope):
                    continue
                # meas/eq ratio
                ratio = (
                    iso.ccSTP_per_g / iso.c_eq_cm3_per_g
                    if not math.isnan(iso.c_eq_cm3_per_g) and iso.c_eq_cm3_per_g > 0
                    else float("nan")
                )
                rows.append((
                    ir.seq_num, ir.inlet_string, device, isotope,
                    iso.extraction_efficiency,
                    iso.ccSTP_true, iso.ccSTP_true_unc,
                    iso.ccSTP_per_g, iso.ccSTP_per_g_unc,
                    iso.c_eq_cm3_per_g, ratio,
                ))

        tbl.setRowCount(len(rows))
        for r, (sn, name, dev, iso,
                eta, ccstp_t, ccstp_t_u,
                per_g, per_g_u, c_eq, ratio) in enumerate(rows):
            bg = _TYPE_BG.get("sample", QColor("#FFFFFF"))
            eta_str = f"{eta:.4f}" if not math.isnan(eta) else "—"
            cells = [
                QTableWidgetItem(str(sn)),
                QTableWidgetItem(name),
                QTableWidgetItem(dev),
                QTableWidgetItem(iso),
                QTableWidgetItem(eta_str),
                QTableWidgetItem(_fmt(ccstp_t)),
                QTableWidgetItem(_fmt(ccstp_t_u)),
                QTableWidgetItem(_fmt(per_g)),
                QTableWidgetItem(_fmt(per_g_u)),
                QTableWidgetItem(_fmt(c_eq)),
                QTableWidgetItem(_fmt(ratio, 3)),
            ]
            for c, it in enumerate(cells):
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                it.setBackground(bg)
                if c in (0, 2, 3):
                    it.setTextAlignment(Qt.AlignCenter)
                elif c >= 4:
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                else:
                    it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                tbl.setItem(r, c, it)

    def _populate_ratios_tab(self) -> None:
        """Populate the Ratios tab with within-inlet ccSTP-based isotope ratios.

        One row per inlet that has ratios. Columns are dynamically built: first
        three are fixed (#, Name, Type), then one pair (value, ±unc) per ratio
        name occurring anywhere in the result. Blanks show only the Df column.
        """
        tbl = self._ratios_tbl
        tbl.setRowCount(0)
        if self._result is None:
            return

        def _fmt(v: float) -> str:
            return f"{v:.4e}" if not math.isnan(v) else "—"

        # Discover all ratio names across all inlets
        all_ratio_names: list = []
        for ir in self._result.inlets:
            for rname in ir.ratios:
                if rname not in all_ratio_names:
                    all_ratio_names.append(rname)

        if not all_ratio_names:
            return

        # Build headers: fixed cols + value/unc pair per ratio
        headers = ["#", "Name", "Type"]
        for rname in all_ratio_names:
            headers.append(rname)
            headers.append(f"± {rname}")
        tbl.setColumnCount(len(headers))
        tbl.setHorizontalHeaderLabels(headers)
        tbl.horizontalHeader().setStyleSheet(_HDR_SS)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for _c in range(len(headers)):
            if _c != 1:
                tbl.horizontalHeader().setSectionResizeMode(_c, QHeaderView.ResizeToContents)

        prep_by_seq = {p.seq_num: p for p in self._seq.inlets} if self._seq else {}

        rows_data: list = []
        for ir in self._result.inlets:
            prep = prep_by_seq.get(ir.seq_num)
            if getattr(prep, "is_supplemental", False):
                continue
            if not ir.ratios:
                continue
            rows_data.append(ir)

        tbl.setRowCount(len(rows_data))
        for r, ir in enumerate(rows_data):
            itype = ir.inlet_type
            bg = _TYPE_BG.get(itype, QColor("#FFFFFF"))
            cells = [
                QTableWidgetItem(str(ir.seq_num)),
                QTableWidgetItem(ir.inlet_string),
                QTableWidgetItem(itype.capitalize()),
            ]
            # Start with enough columns
            ratio_col_offset = 3
            for rname in all_ratio_names:
                val, unc = ir.ratios.get(rname, (float("nan"), float("nan")))
                cells.append(QTableWidgetItem(_fmt(val)))
                cells.append(QTableWidgetItem(_fmt(unc)))
            for c, it in enumerate(cells):
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                it.setBackground(bg)
                if c in (0, 2):
                    it.setTextAlignment(Qt.AlignCenter)
                elif c >= 3:
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                else:
                    it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                tbl.setItem(r, c, it)

    def _populate_cross_ratios_tab(self) -> None:
        """Populate the Cross Ratios tab with cross-inlet ccSTP-based ratios.

        Only populated when Step 11b ran (extraction info present) and sample
        inlets have matching isotopes across different inlets.
        """
        tbl = self._cross_ratios_tbl
        tbl.setRowCount(0)
        if self._result is None:
            return
        if not self._result.cross_ratios:
            return

        def _fmt(v: float) -> str:
            return f"{v:.4e}" if not math.isnan(v) else "—"

        headers = ["Ratio", "Value", "± Unc", "Num #", "Den #", "Method"]
        tbl.setColumnCount(len(headers))
        tbl.setHorizontalHeaderLabels(headers)
        tbl.horizontalHeader().setStyleSheet(_HDR_SS)
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for _c in (1, 2, 3, 4, 5):
            tbl.horizontalHeader().setSectionResizeMode(_c, QHeaderView.ResizeToContents)

        rows: list = []
        for ratio_name, entries in self._result.cross_ratios.items():
            for entry in entries:
                rows.append((
                    ratio_name, entry.ratio_value, entry.ratio_unc,
                    entry.num_seq_num, entry.den_seq_num, entry.method,
                ))

        tbl.setRowCount(len(rows))
        for r, (rname, val, unc, ns, ds, method) in enumerate(rows):
            cells = [
                QTableWidgetItem(rname),
                QTableWidgetItem(_fmt(val)),
                QTableWidgetItem(_fmt(unc)),
                QTableWidgetItem(str(ns)),
                QTableWidgetItem(str(ds)),
                QTableWidgetItem(method),
            ]
            for c, it in enumerate(cells):
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if c in (1, 2):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                elif c in (3, 4):
                    it.setTextAlignment(Qt.AlignCenter)
                else:
                    it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                tbl.setItem(r, c, it)

    def _supp_seq_nums(self) -> set:
        """Return the set of seq_nums that belong to supplemental inlets."""
        if self._seq is None:
            return set()
        return {p.seq_num for p in self._seq.inlets
                if getattr(p, "is_supplemental", False)}

    # _update_calib_pool_label was removed as redundant

    def _current_iso_key(self) -> Optional[str]:
        return self._combo_isotope.currentData()

    def _draw_blank_fit(self) -> None:
        fig = self._blank_fig
        fig.clear()
        ax = fig.add_subplot(111)
        if self._result is None:
            ax.set_axis_off()
            self._blank_canvas.draw()
            return

        key = self._current_iso_key()
        if not key:
            ax.text(0.5, 0.5, "Select Isotope to view the fits",
                    ha="center", va="center", transform=ax.transAxes,
                    color="#78909C", fontsize=12)
            ax.set_axis_off()
            self._blank_canvas.draw()
            return
        bfit = self._result.blank_fits.get(key)

        if bfit is None or not bfit.blank_times:
            ax.text(0.5, 0.5, "No blank data for this isotope",
                    ha="center", va="center", transform=ax.transAxes, color="gray")
            ax.set_axis_off()
            self._blank_canvas.draw()
            return

        supp = self._supp_seq_nums()
        t0 = min(bfit.blank_times)
        xs_h = [(t - t0) / 3600.0 for t in bfit.blank_times]

        def _split(seq_nums, xs, ys, s):
            px = [x for x, n in zip(xs, seq_nums) if n not in s]
            py = [y for y, n in zip(ys, seq_nums) if n not in s]
            pn = [n for n in seq_nums if n not in s]
            sx = [x for x, n in zip(xs, seq_nums) if n in s]
            sy = [y for y, n in zip(ys, seq_nums) if n in s]
            sn_ = [n for n in seq_nums if n in s]
            return px, py, pn, sx, sy, sn_

        mask = bfit.outlier_mask if bfit.outlier_mask else [False] * len(bfit.blank_times)

        # Split into inlier / outlier subsets, then further by primary / supplemental
        def _split_with_mask(seq_nums, xs, ys, msk, s):
            px  = [x for x, n, m in zip(xs, seq_nums, msk) if n not in s and not m]
            py  = [y for y, n, m in zip(ys, seq_nums, msk) if n not in s and not m]
            pn  = [n for n, m in zip(seq_nums, msk) if n not in s and not m]
            sx_ = [x for x, n, m in zip(xs, seq_nums, msk) if n in s and not m]
            sy_ = [y for y, n, m in zip(ys, seq_nums, msk) if n in s and not m]
            sn_ = [n for n, m in zip(seq_nums, msk) if n in s and not m]
            ox  = [x for x, m in zip(xs, msk) if m]
            oy  = [y for y, m in zip(ys, msk) if m]
            on_ = [n for n, m in zip(seq_nums, msk) if m]
            return px, py, pn, sx_, sy_, sn_, ox, oy, on_

        px, py, pn, sx, sy, sn_, ox, oy, on_ = _split_with_mask(
            bfit.blank_seq_nums, xs_h, bfit.blank_signals, mask, supp)

        uncs = bfit.blank_signal_uncs if getattr(bfit, "blank_signal_uncs", None) else [0.0] * len(bfit.blank_signals)
        pe  = [e for e, n, m in zip(uncs, bfit.blank_seq_nums, mask) if n not in supp and not m]
        se  = [e for e, n, m in zip(uncs, bfit.blank_seq_nums, mask) if n in supp and not m]
        oe  = [e for e, m in zip(uncs, mask) if m]

        sc_p = sc_s = sc_out = None
        if px:
            sc_p = ax.scatter(px, py, color="#1565C0", s=50, zorder=3, label="Blank (primary)")
            ax.errorbar(px, py, yerr=pe, fmt="none", ecolor="#1565C0", elinewidth=1.2, capsize=2, zorder=2)
        if sx:
            sc_s = ax.scatter(sx, sy, color="#F57C00", s=70, marker="^", zorder=4,
                              label="Blank (supplemental ↑)")
            ax.errorbar(sx, sy, yerr=se, fmt="none", ecolor="#F57C00", elinewidth=1.2, capsize=2, zorder=2)
        if ox:
            sc_out = ax.scatter(ox, oy, color="#E53935", s=90, marker="x",
                                linewidths=2, zorder=5, label="Outlier (rejected)")
            ax.errorbar(ox, oy, yerr=oe, fmt="none", ecolor="#E53935", elinewidth=1.2, capsize=2, zorder=2)

        for xi, yi, n, m in zip(xs_h, bfit.blank_signals, bfit.blank_seq_nums, mask):
            ax.annotate(str(n), (xi, yi), textcoords="offset points",
                        xytext=(4, 4), fontsize=7,
                        color="#E53935" if m else ("#F57C00" if n in supp else "#1565C0"))

        _blank_entries = []
        for sc, names in [(sc_p, pn), (sc_s, sn_), (sc_out, on_)]:
            if sc is None:
                continue
            _blank_entries.append(
                (sc, lambda i, x, y, ns=names:
                 f"Inlet #{ns[i]}\nt = {x:.2f} h\nBlank = {y:.4e} A")
            )
        if _blank_entries:
            attach_hover_tooltip(self._blank_canvas, ax, _blank_entries)

        if bfit.degree >= 1 and len(bfit.blank_times) >= 2:
            import numpy as np
            x_line = list(np.linspace(min(bfit.blank_times), max(bfit.blank_times), 80))
            y_line = [_polyval(bfit.coeffs, t) for t in x_line]
            x_h = [(t - t0) / 3600.0 for t in x_line]
            r2_lbl = f", R²={bfit.r_squared:.3f}" if not math.isnan(bfit.r_squared) else ""
            ax.plot(x_h, y_line, "--", color="#E53935", linewidth=1.6,
                    label=f"Fit: {bfit.fit_type}{r2_lbl}")
        else:
            mean_b = _polyval(bfit.coeffs, bfit.blank_times[0])
            ax.axhline(mean_b, linestyle="--", color="#E53935", linewidth=1.4,
                       label=f"Mean = {mean_b:.3e}")

        fit_label = getattr(bfit, "fit_type", "—").capitalize()
        r2_str = f", R²={bfit.r_squared:.3f}" if not math.isnan(bfit.r_squared) else ""
        ax.set_title(f"Blank Fit — {key}  [{fit_label}{r2_str}]", fontsize=10)
        ax.set_xlabel("Time from first blank (h)", fontsize=9)
        ax.set_ylabel("Blank net signal (A)", fontsize=9)
        ax.ticklabel_format(style="sci", axis="y", scilimits=(0, 0))
        ax.tick_params(labelsize=8)
        ax.legend(fontsize=8)
        self._blank_canvas.draw()

    def _draw_signal_progression(self) -> None:
        """Net signal for all inlets across the run for the selected isotope."""
        fig = self._prog_fig
        fig.clear()
        ax = fig.add_subplot(111)
        if self._result is None or self._seq is None:
            ax.set_axis_off()
            self._prog_canvas.draw()
            return

        key = self._current_iso_key()
        if not key:
            ax.text(0.5, 0.5, "Select Isotope to view the fits",
                    ha="center", va="center", transform=ax.transAxes,
                    color="#78909C", fontsize=12)
            ax.set_axis_off()
            self._prog_canvas.draw()
            return

        pr = self._result
        inlet_times_lv = {p.seq_num: p.lv_time_start for p in self._seq.inlets}
        t0_global = min(inlet_times_lv.values()) if inlet_times_lv else 0.0
        prep_by_seq = {p.seq_num: p for p in self._seq.inlets}
        supp = self._supp_seq_nums()

        _type_colors = {"blank": "#1565C0", "standard": "#6A1B9A", "sample": "#2E7D32"}
        has_data = False
        for ir in pr.inlets:
            if key not in ir.isotopes:
                continue
            iso = ir.isotopes[key]
            val = iso.net_signal
            val_unc = iso.net_unc
            if math.isnan(val):
                continue
            t_h = (inlet_times_lv.get(ir.seq_num, t0_global) - t0_global) / 3600.0
            is_supp = ir.seq_num in supp
            if is_supp:
                ax.scatter([t_h], [val], marker="^", color="#F57C00", s=60,
                           zorder=4, linewidths=0)
                if not math.isnan(val_unc):
                    ax.errorbar([t_h], [val], yerr=[val_unc], fmt="none", ecolor="#F57C00", elinewidth=1.2, capsize=2, zorder=2)
            else:
                color = _type_colors.get(ir.inlet_type, "#888")
                ax.scatter([t_h], [val], color=color, s=40, zorder=3)
                if not math.isnan(val_unc):
                    ax.errorbar([t_h], [val], yerr=[val_unc], fmt="none", ecolor=color, elinewidth=1.2, capsize=2, zorder=2)
            ann_color = "#F57C00" if is_supp else _type_colors.get(ir.inlet_type, "#888")
            ax.annotate(str(ir.seq_num), (t_h, val), textcoords="offset points",
                        xytext=(4, 4), fontsize=7, color=ann_color)
            has_data = True

        if not has_data:
            ax.text(0.5, 0.5, "No data for this isotope",
                    ha="center", va="center", transform=ax.transAxes, color="gray")
            ax.set_axis_off()
            self._prog_canvas.draw()
            return

        from matplotlib.lines import Line2D
        legend_handles = [
            Line2D([0], [0], marker="o", color="w", markerfacecolor=c,
                   markersize=8, label=t.capitalize())
            for t, c in _type_colors.items()
        ]
        if supp:
            legend_handles.append(
                Line2D([0], [0], marker="^", color="w", markerfacecolor="#F57C00",
                       markersize=9, label="Supplemental")
            )
        ax.legend(handles=legend_handles, fontsize=8, loc="best")
        ax.set_title(f"Signal Progression — {key}", fontsize=10)
        ax.set_xlabel("Time from run start (h)", fontsize=9)
        ax.set_ylabel("Net signal (A)", fontsize=9)
        ax.ticklabel_format(style="sci", axis="y", scilimits=(0, 0))
        ax.tick_params(labelsize=8)
        self._prog_canvas.draw()

    def _draw_qc_chart(self) -> None:
        """Dispatch to the selected QC sub-chart (repro / blank_vs_cc / drift_lin_vs_cc)."""
        fig = self._qc_fig
        fig.clear()
        ax = fig.add_subplot(111)
        if self._result is None:
            ax.set_axis_off()
            self._qc_canvas.draw()
            return

        key = self._current_iso_key()
        if not key:
            ax.text(0.5, 0.5, "Select Isotope to view the fits",
                    ha="center", va="center", transform=ax.transAxes,
                    color="#78909C", fontsize=12)
            ax.set_axis_off()
            self._qc_canvas.draw()
            return

        mode = self._combo_qc_mode.currentData() or "repro"
        pr = self._result
        if mode == "blank_vs_cc":
            self._draw_qc_signal_vs_cc(ax, pr, key, use_corrected=False)
        elif mode == "drift_lin_vs_cc":
            self._draw_qc_signal_vs_cc(ax, pr, key, use_corrected=True)
        else:
            self._draw_qc_repro(ax, pr, key)
        self._qc_canvas.draw()

    def _draw_qc_repro(self, ax, pr, key: str) -> None:
        """% deviation of each standard's sensitivity from the sequence mean."""
        df = pr.drift_fits.get(key)
        mean_s = pr.sensitivities.get(key, float("nan"))

        if df is None or not df.std_sensitivities or math.isnan(mean_s) or mean_s == 0:
            ax.text(0.5, 0.5, "No standard data for this isotope",
                    ha="center", va="center", transform=ax.transAxes, color="gray")
            ax.set_axis_off()
            return

        supp = self._supp_seq_nums()
        rel_devs = [(s - mean_s) / mean_s * 100.0 for s in df.std_sensitivities]
        xs = list(range(1, len(rel_devs) + 1))
        face_colors = ["#D32F2F" if abs(d) > 5.0 else "#2E7D32" for d in rel_devs]
        edge_colors = [
            "#F57C00" if sn in supp else "none"
            for sn in df.std_seq_nums
        ]
        edge_widths = [1.5 if sn in supp else 0.0 for sn in df.std_seq_nums]
        for x, d, fc, ec, ew in zip(xs, rel_devs, face_colors, edge_colors, edge_widths):
            ax.bar(x, d, color=fc, edgecolor=ec, linewidth=ew, width=0.5, zorder=3)
        ax.axhline(0, color="#333", linewidth=0.9)
        ax.axhline(5.0, linestyle="--", color="#FFA726", linewidth=1.0, label="±5% limit")
        ax.axhline(-5.0, linestyle="--", color="#FFA726", linewidth=1.0)
        ax.set_xticks(xs)
        ax.set_xticklabels([str(sn) for sn in df.std_seq_nums], fontsize=8)
        ax.set_title(f"QC — Standard Reproducibility — {key}", fontsize=10)
        ax.set_xlabel("Standard inlet #", fontsize=9)
        ax.set_ylabel("Deviation from mean (%)", fontsize=9)
        ax.tick_params(labelsize=8)
        if supp:
            from matplotlib.patches import Patch
            ax.legend(
                handles=[
                    *ax.get_legend_handles_labels()[0],
                    Patch(facecolor="#888", edgecolor="#F57C00", linewidth=1.5,
                          label="Supplemental"),
                ],
                fontsize=8,
            )
        else:
            ax.legend(fontsize=8)

    def _draw_qc_signal_vs_cc(self, ax, pr, key: str, use_corrected: bool) -> None:
        """
        Regression diagnostic: signal level (X) vs expected/calculated gas
        quantity (Y), for standards (expected certified amount) and samples
        (final calculated ccSTP). A tight linear fit through the origin
        confirms the calibration/correction chain is behaving.

        use_corrected=False: X = raw blank-corrected signal ("blank_vs_cc").
        use_corrected=True:  X = drift+linearity-corrected signal
        ("drift_lin_vs_cc") -- evaluates the drift/linearity fits at each
        inlet the same way _draw_drift_correction()/_draw_linearity() do,
        reusing their exact fit coefficients so this stays consistent with
        those tabs. Deliberately omits web's K4 rescaling constant (a
        display-only scale factor with no equivalent anywhere in the shared
        core pipeline, so its provenance can't be verified) -- the
        regression's R² and linearity are unaffected by a constant x-scale,
        only the absolute axis numbers differ from web's.
        """
        from ngam_protocol_processor import _expval as _proc_expval

        df = pr.drift_fits.get(key) if use_corrected else None
        lf = pr.linearity_fits.get(key) if use_corrected else None

        # lv_time_start lives on the parsed InletPrep (self._seq.inlets), not
        # on InletProcessingResult -- cross-reference by seq_num, same as
        # _draw_drift_correction()/_draw_linearity() do for their own charts.
        _t_by_seq: Dict[int, float] = {
            prep.seq_num: prep.lv_time_start for prep in (self._seq.inlets if self._seq else [])
        }

        std_x, std_y, std_t = [], [], []
        smp_x, smp_y, smp_t = [], [], []

        for ir in (pr.inlets or []):
            iso = ir.isotopes.get(key)
            if iso is None or math.isnan(iso.blank_corrected):
                continue

            x = iso.blank_corrected
            if use_corrected:
                drift_val = 1.0
                if df is not None and df.coeffs:
                    t = _t_by_seq.get(ir.seq_num)
                    if t:
                        drift_val = (
                            _proc_expval(df.coeffs, t)
                            if getattr(df, "fit_type", "") == "exponential"
                            else _polyval(df.coeffs, t)
                        )
                lin_val = 1.0
                if lf is not None and lf.coeffs and lf.fit_type not in ("none", "mean"):
                    lin_val = _polyval(lf.coeffs, x)
                if lin_val:
                    x = (x * drift_val) / lin_val

            if ir.is_repro_ref or ir.is_lin_ref:
                amt = ir.reference_amounts.get(key, ir.reference_amount)
                if amt and amt > 0:
                    std_x.append(x)
                    std_y.append(amt)
                    std_t.append(ir.seq_num)
            elif ir.inlet_type == "sample":
                final_v = iso.linearity_ccSTP
                if math.isnan(final_v):
                    final_v = iso.drift_ccSTP if not math.isnan(iso.drift_ccSTP) else iso.ccSTP
                if final_v and final_v > 0:
                    smp_x.append(x)
                    smp_y.append(final_v)
                    smp_t.append(ir.seq_num)

        if not std_x and not smp_x:
            ax.text(0.5, 0.5, "No standard/sample data for this isotope",
                    ha="center", va="center", transform=ax.transAxes, color="gray")
            ax.set_axis_off()
            return

        if std_x:
            ax.scatter(std_x, std_y, s=36, color="#EF4444", marker="s", label="Standards", zorder=3)
        if smp_x:
            ax.scatter(smp_x, smp_y, s=36, color="#3B82F6", marker="s", label="Samples", zorder=3)

        if len(std_x) >= 2:
            import numpy as np
            slope, intercept = np.polyfit(std_x, std_y, 1)
            all_x = std_x + smp_x
            x0, x1 = 0.0, max(all_x) * 1.05
            ax.plot([x0, x1], [slope * x0 + intercept, slope * x1 + intercept],
                    color="#666", linewidth=1.2, linestyle="-", zorder=2)
            pred = [slope * x + intercept for x in std_x]
            ss_res = sum((y - p) ** 2 for y, p in zip(std_y, pred))
            ss_tot = sum((y - (sum(std_y) / len(std_y))) ** 2 for y in std_y)
            r2 = 1 - ss_res / ss_tot if ss_tot else float("nan")
            ax.text(0.05, 0.95,
                    f"y = {slope:.4e}x {'+' if intercept >= 0 else '-'} {abs(intercept):.4e}\nR² = {r2:.4f}",
                    transform=ax.transAxes, ha="left", va="top", fontsize=8,
                    bbox=dict(boxstyle="round", facecolor="#FAFAFA", edgecolor="#CCC"))

        xlabel = "Drift & Linearity Corrected Signal (A)" if use_corrected else "Blank Corrected Signal (A)"
        ax.set_title(f"QC — {xlabel} vs Gas Quantity — {key}", fontsize=10)
        ax.set_xlabel(xlabel, fontsize=9)
        ax.set_ylabel("Gas Quantity (ccSTP)", fontsize=9)
        ax.ticklabel_format(style="sci", axis="both", scilimits=(0, 0))
        ax.tick_params(labelsize=8)
        ax.legend(fontsize=8)

    # ------------------------------------------------------------------
    # Popup windows for SMS / QMS raw fit viewers
    # ------------------------------------------------------------------

    def _ensure_inlet_sig_popup(self) -> _FitPopup:
        if self._inlet_sig_popup is None:
            popup = _FitPopup(
                "Inlet Signals",
                content=self._inlet_signals_widget,
                return_to=self,
            )
            screen = QApplication.primaryScreen().availableGeometry()
            popup.resize(min(screen.width() - 80, 1600), min(screen.height() - 80, 950))
            self._inlet_sig_popup = popup
        return self._inlet_sig_popup

    def _draw_inlet_signals(self, row_idx: Optional[int] = None) -> None:
        if self._seq is None or self._result is None:
            return
        if row_idx is None:
            row_idx = self._current_inlet_idx()
        if row_idx < 0 or row_idx >= len(self._seq.inlets):
            return
        prep = self._seq.inlets[row_idx]
        ir   = self._result.inlets[row_idx]
        self._inlet_signals_widget.load(prep, ir)
        popup = self._ensure_inlet_sig_popup()
        popup.setWindowTitle(
            f"Inlet Signals — Inlet {prep.seq_num}: {prep.inlet_string or 'unknown'}"
        )
        if self._seq:
            popup.set_inlets(self._seq.inlets, row_idx, self._draw_inlet_signals)
        popup.show()
        popup.raise_()
        popup.activateWindow()

    def _ensure_sms_popup(self) -> _FitPopup:
        if self._sms_popup is None:
            sms_w = QWidget()
            sms_root = QVBoxLayout(sms_w)
            sms_root.setContentsMargins(4, 4, 4, 0)
            sms_root.setSpacing(4)

            # outlier threshold control strip
            ctrl_row = QHBoxLayout()
            ctrl_row.setSpacing(6)
            ctrl_row.addWidget(QLabel("Outlier σ:"))
            self._sms_nsigma_spin = QDoubleSpinBox()
            self._sms_nsigma_spin.setRange(1.0, 10.0)
            self._sms_nsigma_spin.setValue(3.0)
            self._sms_nsigma_spin.setSingleStep(0.5)
            self._sms_nsigma_spin.setDecimals(1)
            self._sms_nsigma_spin.setFixedWidth(70)
            self._sms_nsigma_spin.setToolTip(
                "N-sigma threshold for outlier rejection on residuals\n"
                "Lower = stricter (more points removed)"
            )
            self._sms_nsigma_spin.valueChanged.connect(
                lambda _: self._draw_sms_raw_fit()
            )
            ctrl_row.addWidget(self._sms_nsigma_spin)
            ctrl_row.addStretch()
            sms_root.addLayout(ctrl_row)

            # panels row
            panels_w = QWidget()
            panels_lay = QHBoxLayout(panels_w)
            panels_lay.setContentsMargins(0, 0, 0, 0)
            panels_lay.setSpacing(4)
            panels_lay.addWidget(self._sms_panel_4he)
            panels_lay.addWidget(self._sms_panel_3he)
            sms_root.addWidget(panels_w, 1)

            popup = _FitPopup("SMS Raw Fit", content=sms_w, return_to=self)
            screen = QApplication.primaryScreen().availableGeometry()
            popup.resize(min(screen.width() - 80, 1600), min(screen.height() - 80, 950))
            self._sms_popup = popup
        return self._sms_popup

    def _ensure_qms_popup(self) -> _FitPopup:
        if self._qms_popup is None:
            popup = _FitPopup("QMS Raw Fit", content=self._qms_sub_tabs, return_to=self)
            screen = QApplication.primaryScreen().availableGeometry()
            popup.resize(min(screen.width() - 80, 1600), min(screen.height() - 80, 950))
            self._qms_popup = popup
        return self._qms_popup

    def _draw_sms_raw_fit(self, row_idx: Optional[int] = None) -> None:
        """Parse the SMS file for the selected inlet and populate both fit panels."""
        if self._seq is None:
            return
        if row_idx is None:
            row_idx = self._current_inlet_idx()
        if row_idx < 0 or row_idx >= len(self._seq.inlets):
            return

        prep = self._seq.inlets[row_idx]
        ms = prep.ms_by_device.get("SMS")
        if ms is None or ms.resolved_path is None:
            log.debug("No SMS file for inlet %d", prep.seq_num)
            return

        path = ms.resolved_path
        try:
            sms_data = parse_sms(path)
        except Exception as exc:
            log.error("SMS parse failed for %s: %s", path, exc)
            return

        label_prefix = f"Inlet {prep.seq_num}: {prep.inlet_string or 'unknown'}"
        nsigma = getattr(self, "_sms_nsigma_spin", None)
        nsigma = nsigma.value() if nsigma is not None else 3.0
        
        model_4he = self._fit_model_overrides.get(prep.seq_num, {}).get("SMS:4He", "Auto")
        model_3he = self._fit_model_overrides.get(prep.seq_num, {}).get("SMS:3He", "Auto")
        
        try:
            r4 = fit_inlet(sms_data, "4He", model_4he, outlier_nsigma=nsigma)
            self._sms_panel_4he.load(r4, f"4He — {label_prefix}")
        except Exception as exc:
            log.error("4He fit failed: %s", exc)

        try:
            r3 = fit_inlet(sms_data, "3He", model_3he, outlier_nsigma=nsigma)
            self._sms_panel_3he.load(r3, f"3He — {label_prefix}")
        except Exception as exc:
            log.error("3He fit failed: %s", exc)

        popup = self._ensure_sms_popup()
        popup.setWindowTitle(f"SMS Raw Fit — {label_prefix}")
        if self._seq:
            popup.set_inlets(self._seq.inlets, row_idx, self._draw_sms_raw_fit)
        popup.show()
        popup.raise_()
        popup.activateWindow()

    def _draw_qms_raw_fit(self, row_idx: Optional[int] = None) -> None:
        if self._seq is None:
            return
        if row_idx is None:
            row_idx = self._current_inlet_idx()
        if row_idx < 0 or row_idx >= len(self._seq.inlets):
            return

        prep = self._seq.inlets[row_idx]

        _panel_map = {
            "QMSAr":   self._qms_panel_ar,
            "QMSNe":   self._qms_panel_ne,
            "QMSKrXe": self._qms_panel_krxe,
        }

        for device, panel in _panel_map.items():
            ms = prep.ms_by_device.get(device)
            if ms is None or ms.resolved_path is None:
                log.debug("No %s file for inlet %d", device, prep.seq_num)
                continue

            try:
                qms_data = parse_qms(ms.resolved_path)
            except Exception as exc:
                log.error("QMS parse failed for %s: %s", ms.resolved_path, exc)
                continue

            fits = {}
            for iso in qms_data.isotopes:
                key = f"{device}:{iso}"
                model = self._fit_model_overrides.get(prep.seq_num, {}).get(key, "Auto")
                try:
                    fits[iso] = fit_qms_isotope(qms_data, iso, model)
                except Exception as exc:
                    log.error("QMS fit failed for %s %s: %s", device, iso, exc)

            try:
                ratios = compute_qms_ratios(fits, device)
            except Exception as exc:
                log.error("QMS ratio computation failed for %s: %s", device, exc)
                ratios = {}

            try:
                panel.load(qms_data, fits, ratios)
            except Exception as exc:
                log.error("QMSFitWidget.load failed for %s: %s", device, exc)

        popup = self._ensure_qms_popup()
        label_prefix = f"Inlet {prep.seq_num}: {prep.inlet_string or 'unknown'}"
        popup.setWindowTitle(f"QMS Raw Fit — {label_prefix}")
        if self._seq:
            popup.set_inlets(self._seq.inlets, row_idx, self._draw_qms_raw_fit)
        popup.show()
        popup.raise_()
        popup.activateWindow()

    def _ensure_gauge_inlet_popup(self) -> _FitPopup:
        if self._gauge_inlet_popup is None:
            popup = _FitPopup("Gauge Inlet Detail", self._detail_canvas, self)
            popup.resize(920, 500)
            self._gauge_inlet_popup = popup
        return self._gauge_inlet_popup

    def _draw_gauge_inlet(self, row_idx: Optional[int] = None) -> None:
        if self._seq is None or self._result is None:
            return
        if row_idx is None:
            row_idx = self._current_inlet_idx()
        if row_idx < 0 or row_idx >= len(self._seq.inlets):
            return
        prep = self._seq.inlets[row_idx]
        self._refresh_gauge_inlet(prep.seq_num)
        popup = self._ensure_gauge_inlet_popup()
        popup.setWindowTitle(
            f"Gauge Inlet Detail — #{prep.seq_num}: {prep.inlet_string or 'unknown'}"
        )
        popup.set_inlets(self._seq.inlets, row_idx, self._draw_gauge_inlet)
        popup.show()
        popup.raise_()
        popup.activateWindow()

    def _draw_drift_correction(self) -> None:
        """
        Two-panel chart:
          Top  — ccSTP values: mean-sensitivity (blue) vs drift-corrected (orange)
                 for sample inlets, plotted against run time.
          Bottom — drift sensitivity S(t) overlay from the DriftFit, with standard
                 points as dots and the interpolated curve.
        """
        fig = self._drift_fig
        fig.clear()
        if self._result is None or self._seq is None:
            self._drift_canvas.draw()
            return

        key = self._current_iso_key()
        if not key:
            ax = fig.add_subplot(111)
            ax.text(0.5, 0.5, "Select Isotope to view the fits",
                    ha="center", va="center", transform=ax.transAxes,
                    color="#78909C", fontsize=12)
            ax.set_axis_off()
            self._drift_canvas.draw()
            return
        pr  = self._result
        df  = pr.drift_fits.get(key)

        if df is None:
            ax = fig.add_subplot(111)
            ax.text(0.5, 0.5, "No drift data for this isotope",
                    ha="center", va="center", transform=ax.transAxes, color="gray")
            ax.set_axis_off()
            self._drift_canvas.draw()
            return

        inlet_times_lv = {p.seq_num: p.lv_time_start for p in self._seq.inlets}
        t0 = min(inlet_times_lv.values()) if inlet_times_lv else 0.0

        ax_top, ax_bot = fig.subplots(2, 1, sharex=False,
                                       gridspec_kw={"height_ratios": [3, 2]})

        # ── Top: ccSTP comparison ──────────────────────────────────────
        prep_by_seq = {p.seq_num: p for p in self._seq.inlets}
        has_drift = False
        for ir in pr.inlets:
            prep = prep_by_seq.get(ir.seq_num)
            if getattr(prep, "is_supplemental", False):
                continue
            if ir.inlet_type != "sample":
                continue
            if key not in ir.isotopes:
                continue
            iso = ir.isotopes[key]
            t_h = (inlet_times_lv.get(ir.seq_num, t0) - t0) / 3600.0

            mean_v = iso.ccSTP
            mean_v_unc = iso.ccSTP_unc
            drift_v = iso.drift_ccSTP
            drift_v_unc = iso.drift_ccSTP_unc

            if not math.isnan(mean_v):
                ax_top.scatter([t_h], [mean_v], color="#1565C0", s=45, zorder=4,
                               label="Mean sens." if not has_drift else "")
                if not math.isnan(mean_v_unc):
                    ax_top.errorbar([t_h], [mean_v], yerr=[mean_v_unc], fmt="none", ecolor="#1565C0", elinewidth=1.0, capsize=2, zorder=2)
                if not math.isnan(drift_v):
                    ax_top.scatter([t_h], [drift_v], color="#E65100", s=45, zorder=4,
                                   marker="D",
                                   label="Drift corr." if not has_drift else "")
                    if not math.isnan(drift_v_unc):
                        ax_top.errorbar([t_h], [drift_v], yerr=[drift_v_unc], fmt="none", ecolor="#E65100", elinewidth=1.0, capsize=2, zorder=2)
                    ax_top.annotate("", xy=(t_h, drift_v), xytext=(t_h, mean_v),
                                    arrowprops=dict(arrowstyle="-", color="#90A4AE",
                                                    lw=1.2))
                    has_drift = True
            ax_top.annotate(str(ir.seq_num), (t_h, mean_v if not math.isnan(mean_v) else drift_v),
                            textcoords="offset points", xytext=(4, 3),
                            fontsize=7, color="#37474F")

        fit_label = getattr(df, "fit_type", "").capitalize()
        r2_str = f"  R²={df.r_squared:.3f}" if not math.isnan(df.r_squared) else ""
        ax_top.set_title(
            f"Drift Correction — {key}  [{fit_label}{r2_str}]", fontsize=10
        )
        ax_top.set_xlabel("Time from run start (h)", fontsize=8)
        ax_top.set_ylabel("ccSTP", fontsize=8)
        ax_top.ticklabel_format(style="sci", axis="y", scilimits=(0, 0))
        ax_top.tick_params(labelsize=7)
        if has_drift:
            ax_top.legend(fontsize=7, loc="best")

        # ── Bottom: sensitivity S(t) — standards scatter + fit curve ──
        ax_bot.set_title("Sensitivity S(t)", fontsize=9)
        _tip_entries = []
        if df.std_times:
            from ngam_protocol_processor import _expval as _proc_expval
            supp = self._supp_seq_nums()
            t0_s = min(df.std_times)
            xs_h = [(t - t0_s) / 3600.0 for t in df.std_times]
            seq_nums_std = list(getattr(df, "std_seq_nums", [None] * len(df.std_times)))
            d_mask = df.outlier_mask if df.outlier_mask else [False] * len(df.std_times)
            uncs = df.std_sensitivity_uncs if getattr(df, "std_sensitivity_uncs", None) else [0.0] * len(df.std_sensitivities)
            prim_x = [x for x, sn, m in zip(xs_h, seq_nums_std, d_mask) if sn not in supp and not m]
            prim_y = [y for y, sn, m in zip(df.std_sensitivities, seq_nums_std, d_mask) if sn not in supp and not m]
            prim_e = [e for e, sn, m in zip(uncs, seq_nums_std, d_mask) if sn not in supp and not m]
            prim_n = [sn for sn, m in zip(seq_nums_std, d_mask) if sn not in supp and not m]
            supp_x = [x for x, sn, m in zip(xs_h, seq_nums_std, d_mask) if sn in supp and not m]
            supp_y = [y for y, sn, m in zip(df.std_sensitivities, seq_nums_std, d_mask) if sn in supp and not m]
            supp_e = [e for e, sn, m in zip(uncs, seq_nums_std, d_mask) if sn in supp and not m]
            supp_n = [sn for sn, m in zip(seq_nums_std, d_mask) if sn in supp and not m]
            out_x  = [x for x, m in zip(xs_h, d_mask) if m]
            out_y  = [y for y, m in zip(df.std_sensitivities, d_mask) if m]
            out_e  = [e for e, m in zip(uncs, d_mask) if m]
            out_n  = [sn for sn, m in zip(seq_nums_std, d_mask) if m]
            sc_p = sc_s = sc_out = None
            if prim_x:
                sc_p = ax_bot.scatter(prim_x, prim_y, color="#2E7D32", s=35,
                                      zorder=3, label="Standards")
                ax_bot.errorbar(prim_x, prim_y, yerr=prim_e, fmt="none", ecolor="#2E7D32", elinewidth=1.0, capsize=2, zorder=2)
            if supp_x:
                sc_s = ax_bot.scatter(supp_x, supp_y, marker="^", color="#F57C00", s=55,
                                      zorder=4, label="Supp. standards")
                ax_bot.errorbar(supp_x, supp_y, yerr=supp_e, fmt="none", ecolor="#F57C00", elinewidth=1.0, capsize=2, zorder=2)
            if out_x:
                sc_out = ax_bot.scatter(out_x, out_y, color="#E53935", s=80, marker="x",
                                        linewidths=2, zorder=5, label="Outlier (rejected)")
                ax_bot.errorbar(out_x, out_y, yerr=out_e, fmt="none", ecolor="#E53935", elinewidth=1.0, capsize=2, zorder=2)
            for sc, ns in [(sc_p, prim_n), (sc_s, supp_n), (sc_out, out_n)]:
                if sc is None:
                    continue
                _tip_entries.append(
                    (sc, lambda i, x, y, ns=ns:
                     f"Inlet #{ns[i]}\nt = {x:.2f} h\nS = {y:.4e} A/ccSTP")
                )

            # Fit line
            fit_type = getattr(df, "fit_type", "")
            if fit_type != "mean" and len(df.std_times) >= 2:
                import numpy as np
                x_line = list(np.linspace(min(df.std_times), max(df.std_times), 80))
                if fit_type == "exponential":
                    y_line = [_proc_expval(df.coeffs, t) for t in x_line]
                else:
                    y_line = [_polyval(df.coeffs, t) for t in x_line]
                x_h = [(t - t0_s) / 3600.0 for t in x_line]
                ax_bot.plot(x_h, y_line, "--", color="#E65100", lw=1.4,
                            label=f"Fit: {fit_type}")
            else:
                mean_s = pr.sensitivities.get(key, float("nan"))
                if not math.isnan(mean_s):
                    ax_bot.axhline(mean_s, linestyle=":", color="#90A4AE", lw=1.2,
                                   label=f"Mean = {mean_s:.3e}")

        if _tip_entries:
            attach_hover_tooltip(self._drift_canvas, ax_bot, _tip_entries)

        ax_bot.set_xlabel("Time from first std (h)", fontsize=8)
        ax_bot.set_ylabel("S (A/ccSTP)", fontsize=8)
        ax_bot.ticklabel_format(style="sci", axis="y", scilimits=(0, 0))
        ax_bot.tick_params(labelsize=7)
        ax_bot.legend(fontsize=7)
        self._drift_canvas.draw()

    def _draw_linearity(self) -> None:
        """
        Two-panel chart:
          Top  — Sensitivity S vs blank-corrected signal level for standards,
                 with the linearity fit curve.  Shows whether the detector
                 response is constant across signal levels.
          Bottom — ccSTP comparison: mean-sensitivity vs linearity-corrected
                 for each sample (only when linearity correction is active).
        """
        fig = self._lin_fig
        fig.clear()
        if self._result is None or self._seq is None:
            self._lin_canvas.draw()
            return

        key = self._current_iso_key()
        if not key:
            ax = fig.add_subplot(111)
            ax.text(0.5, 0.5, "Select Isotope to view the fits",
                    ha="center", va="center", transform=ax.transAxes,
                    color="#78909C", fontsize=12)
            ax.set_axis_off()
            self._lin_canvas.draw()
            return
        pr  = self._result
        lf  = pr.linearity_fits.get(key)

        if lf is None or not lf.signal_levels:
            ax = fig.add_subplot(111)
            ax.text(0.5, 0.5, "No linearity data for this isotope",
                    ha="center", va="center", transform=ax.transAxes, color="gray")
            ax.set_axis_off()
            self._lin_canvas.draw()
            return

        has_lin_corr = lf.fit_type != "none"
        ax_top, ax_bot = fig.subplots(2, 1)

        # ── Top: S vs bc signal (linearity assessment) ─────────────────
        l_mask = lf.outlier_mask if lf.outlier_mask else [False] * len(lf.signal_levels)
        l_uncs = lf.sensitivity_uncs if getattr(lf, "sensitivity_uncs", None) else [0.0] * len(lf.sensitivities)
        inl_x = [x for x, m in zip(lf.signal_levels, l_mask) if not m]
        inl_y = [y for y, m in zip(lf.sensitivities,  l_mask) if not m]
        inl_e = [e for e, m in zip(l_uncs, l_mask) if not m]
        out_x = [x for x, m in zip(lf.signal_levels, l_mask) if m]
        out_y = [y for y, m in zip(lf.sensitivities,  l_mask) if m]
        out_e = [e for e, m in zip(l_uncs, l_mask) if m]
        if inl_x:
            ax_top.scatter(inl_x, inl_y, color="#6A1B9A", s=50, zorder=3, label="Standards")
            ax_top.errorbar(inl_x, inl_y, yerr=inl_e, fmt="none", ecolor="#6A1B9A", elinewidth=1.0, capsize=2, zorder=2)
        if out_x:
            ax_top.scatter(out_x, out_y, color="#E53935", s=80, marker="x",
                           linewidths=2, zorder=5, label="Outlier (rejected)")
            ax_top.errorbar(out_x, out_y, yerr=out_e, fmt="none", ecolor="#E53935", elinewidth=1.0, capsize=2, zorder=2)
        for xi, yi, sn, m in zip(lf.signal_levels, lf.sensitivities, lf.std_seq_nums, l_mask):
            ax_top.annotate(str(sn), (xi, yi), textcoords="offset points",
                            xytext=(4, 3), fontsize=7,
                            color="#E53935" if m else "#6A1B9A")

        # Fit curve or mean line
        if has_lin_corr and len(lf.signal_levels) >= 2:
            import numpy as np
            x_range = max(lf.signal_levels) - min(lf.signal_levels)
            x_lin = list(np.linspace(
                min(lf.signal_levels) - 0.05 * x_range,
                max(lf.signal_levels) + 0.05 * x_range,
                80
            ))
            y_lin = [_polyval(lf.coeffs, x) for x in x_lin]
            r2_str = f"  R²={lf.r_squared:.3f}" if not math.isnan(lf.r_squared) else ""
            ax_top.plot(x_lin, y_lin, "--", color="#E53935", lw=1.5,
                        label=f"Fit: {lf.fit_type.capitalize()}{r2_str}")

            # Show where sample signals fall
            prep_by_seq = {p.seq_num: p for p in self._seq.inlets}
            sample_bcs = [
                ir.isotopes[key].blank_corrected
                for ir in pr.inlets
                if key in ir.isotopes
                and ir.inlet_type == "sample"
                and not getattr(prep_by_seq.get(ir.seq_num), "is_supplemental", False)
                and not math.isnan(ir.isotopes[key].blank_corrected)
            ]
            if sample_bcs:
                ax_top.axvspan(min(sample_bcs), max(sample_bcs),
                               alpha=0.08, color="#1565C0", label="Sample range")
        else:
            mean_s = pr.sensitivities.get(key, float("nan"))
            if not math.isnan(mean_s):
                ax_top.axhline(mean_s, linestyle=":", color="#90A4AE", lw=1.3,
                               label=f"Mean S = {mean_s:.3e}")

        fit_label = lf.fit_type.capitalize() if has_lin_corr else "No correction"
        ax_top.set_title(f"Linearity — {key}  [{fit_label}]", fontsize=10)
        ax_top.set_xlabel("Blank-corrected signal (A)", fontsize=8)
        ax_top.set_ylabel("Sensitivity (A/ccSTP)", fontsize=8)
        ax_top.ticklabel_format(style="sci", axis="both", scilimits=(0, 0))
        ax_top.tick_params(labelsize=7)
        ax_top.legend(fontsize=7)

        # ── Bottom: ccSTP before/after linearity correction ───────────
        prep_by_seq = {p.seq_num: p for p in self._seq.inlets}
        has_any = False
        for ir in pr.inlets:
            if ir.inlet_type != "sample":
                continue
            prep = prep_by_seq.get(ir.seq_num)
            if getattr(prep, "is_supplemental", False):
                continue
            if key not in ir.isotopes:
                continue
            iso = ir.isotopes[key]
            x = ir.seq_num
            mean_v = iso.ccSTP
            mean_v_unc = iso.ccSTP_unc
            lin_v  = iso.linearity_ccSTP
            lin_v_unc = iso.linearity_ccSTP_unc

            if not math.isnan(mean_v):
                ax_bot.scatter([x], [mean_v], color="#1565C0", s=40, zorder=4,
                               label="Mean sens." if not has_any else "")
                if not math.isnan(mean_v_unc):
                    ax_bot.errorbar([x], [mean_v], yerr=[mean_v_unc], fmt="none", ecolor="#1565C0", elinewidth=1.0, capsize=2, zorder=2)
            if not math.isnan(lin_v):
                ax_bot.scatter([x], [lin_v], color="#E65100", s=40, marker="D",
                               zorder=4, label="Lin. corr." if not has_any else "")
                if not math.isnan(lin_v_unc):
                    ax_bot.errorbar([x], [lin_v], yerr=[lin_v_unc], fmt="none", ecolor="#E65100", elinewidth=1.0, capsize=2, zorder=2)
                if not math.isnan(mean_v):
                    ax_bot.plot([x, x], [mean_v, lin_v], color="#B0BEC5", lw=1.0)
            if not math.isnan(mean_v) or not math.isnan(lin_v):
                has_any = True

        if not has_any:
            ax_bot.text(0.5, 0.5,
                        "No linearity correction applied\n(choose Linear/Quadratic in toolbar)",
                        ha="center", va="center", transform=ax_bot.transAxes,
                        color="gray", fontsize=9)
            ax_bot.set_axis_off()
        else:
            ax_bot.set_xlabel("Inlet #", fontsize=8)
            ax_bot.set_ylabel("ccSTP", fontsize=8)
            ax_bot.ticklabel_format(style="sci", axis="y", scilimits=(0, 0))
            ax_bot.tick_params(labelsize=7)
            ax_bot.xaxis.get_major_locator().set_params(integer=True)
            ax_bot.legend(fontsize=7)

        self._lin_canvas.draw()

    # ------------------------------------------------------------------
    # Inlet list
    # ------------------------------------------------------------------

    def _build_inlet_tree(
        self,
        seq: ProtocolSequence,
        inlets,          # List[InletPrep]  — seq.inlets (always)
        result_inlets,   # List[InletProcessingResult] | None — None when previewing
    ) -> None:
        """
        Build (or rebuild) the inlet tree grouped by source protocol file.

        - Top-level items: collapsible group per protocol file
        - Leaf items: one per inlet, UserRole = index into seq.inlets
        """
        tree = self._inlet_tree
        tree.blockSignals(True)
        tree.clear()

        # result lookup: seq_num → InletProcessingResult
        result_by_seq: Dict[int, object] = {}
        if result_inlets:
            result_by_seq = {ir.seq_num: ir for ir in result_inlets}

        # Group inlets by source file (main run first, then supplemental)
        from collections import OrderedDict
        groups: OrderedDict = OrderedDict()
        main_key = seq.protocol_path
        groups[main_key] = []
        for i, prep in enumerate(inlets):
            key = prep.source_protocol if getattr(prep, "is_supplemental", False) and prep.source_protocol else main_key
            groups.setdefault(key, [])
            groups[key].append((i, prep))

        _GRP_BG = QColor("#E8EDF2")
        _GRP_FG = QColor("#1A237E")

        for g_idx, (src_path, members) in enumerate(groups.items()):
            if not members:
                continue

            # ── group header ──────────────────────────────────────────
            fname = os.path.basename(src_path) if src_path else "Unknown"
            is_supp_group = (src_path != main_key)
            grp_item = QTreeWidgetItem(tree)
            grp_item.setText(0, fname)
            grp_item.setForeground(0, _GRP_FG)
            grp_item.setBackground(0, _COL_SUPPLEMENTAL if is_supp_group else _GRP_BG)
            for col in range(1, 5):
                grp_item.setBackground(col, _COL_SUPPLEMENTAL if is_supp_group else _GRP_BG)
            font = grp_item.font(0)
            font.setBold(True)
            grp_item.setFont(0, font)
            grp_item.setFlags(Qt.ItemIsEnabled)   # not selectable
            grp_item.setExpanded(True)
            # Span all columns for the group header
            tree.setFirstColumnSpanned(g_idx, QModelIndex(), True)

            # ── leaf items ────────────────────────────────────────────
            for inlet_idx, prep in members:
                ir = result_by_seq.get(prep.seq_num)
                itype   = ir.inlet_type if ir else getattr(prep, "inlet_type", "unknown")
                is_supp = getattr(prep, "is_supplemental", False)
                bg      = _COL_SUPPLEMENTAL if is_supp else _TYPE_BG.get(itype, QColor("#FFFFFF"))

                leaf = QTreeWidgetItem(grp_item)
                leaf.setData(0, Qt.UserRole, inlet_idx)   # index into seq.inlets
                leaf.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)

                num_label  = f"{prep.seq_num}*" if is_supp else str(prep.seq_num)
                name_label = (ir.inlet_string if ir else prep.inlet_string) or ""
                
                # Append partition step balloon tags if present
                he_steps = 0
                ne_steps = 0
                if ir is not None:
                    he_steps = ir.partition_steps.get("Helium", 0) if ir.partition_steps else 0
                    ne_steps = ir.partition_steps.get("Neon", 0) if ir.partition_steps else 0
                elif hasattr(prep, "partition_steps") and prep.partition_steps:
                    he_steps = prep.partition_steps.get("Helium", 0) or 0
                    ne_steps = prep.partition_steps.get("Neon", 0) or 0
                
                step_tags = []
                if he_steps > 0:
                    step_tags.append(f"He:{he_steps}")
                if ne_steps > 0:
                    step_tags.append(f"Ne:{ne_steps}")
                if step_tags:
                    name_label = f"{name_label}\n🎈 {', '.join(step_tags)}"

                type_label = ("↑ " if is_supp else "") + itype.capitalize()
                dil_val    = self._dilution_factors.get(prep.seq_num)
                if dil_val is None:
                    if ir is not None:
                        dil_val = ir.dilution_factor if not math.isnan(ir.dilution_factor) else 1.0
                    else:
                        dil_val = prep.dilution_factor if not math.isnan(prep.dilution_factor) else 1.0
                ref_txt    = (f"{ir.reference_amount:.4g}" if ir and ir.reference_amount else "—")

                for col, (txt, align) in enumerate([
                    (num_label,  Qt.AlignCenter),
                    (name_label, Qt.AlignLeft | Qt.AlignVCenter),
                    (type_label, Qt.AlignCenter),
                    (f"{dil_val:.1f}", Qt.AlignCenter),
                    (ref_txt,    Qt.AlignCenter),
                ]):
                    leaf.setText(col, txt)
                    leaf.setTextAlignment(col, align)
                    leaf.setBackground(col, bg)

                # Tooltip on std inlets: show certified per-isotope amounts
                if ir and itype == "standard" and ir.reference_amounts:
                    tip_lines = ["Certified amounts (ccSTP):"]
                    for sp, amt in sorted(ir.reference_amounts.items()):
                        tip_lines.append(f"  {sp}: {amt:.4e}")
                    tip = "\n".join(tip_lines)
                    for col in range(5):
                        leaf.setToolTip(col, tip)

                # Visual marking for excluded / force-included standard & blank inlets
                excl_dict = self._excluded_blanks if itype == "blank" else self._excluded_standards
                force_dict = self._force_included_blanks if itype == "blank" else self._force_included_standards
                if itype in ("standard", "blank") and prep.seq_num in excl_dict:
                    excl_entry = excl_dict[prep.seq_num]
                    if excl_entry is None:
                        excl_label = "⊘ excluded (all isotopes)"
                    else:
                        excl_label = f"⊘ excluded ({', '.join(sorted(excl_entry))})"
                    excl_bg = QColor("#FFEBEE")
                    excl_font = QFont()
                    excl_font.setStrikeOut(True)
                    for col in range(5):
                        leaf.setBackground(col, excl_bg)
                        leaf.setFont(col, excl_font)
                    leaf.setToolTip(0, excl_label)
                    leaf.setText(1, f"{name_label}  {excl_label}")
                elif itype in ("standard", "blank") and prep.seq_num in force_dict:
                    force_entry = force_dict[prep.seq_num]
                    if force_entry is None:
                        force_label = "⚑ forced (all isotopes)"
                    else:
                        force_label = f"⚑ forced ({', '.join(sorted(force_entry))})"
                    force_bg = QColor("#E8F5E9")
                    for col in range(5):
                        leaf.setBackground(col, force_bg)
                    leaf.setToolTip(0, force_label)
                    leaf.setText(1, f"{name_label}  {force_label}")

        tree.blockSignals(False)

    # ------------------------------------------------------------------
    # Dilution factor editing
    # ------------------------------------------------------------------

    def set_dilution_factors(self, factors: Dict[int, float]) -> None:
        """Update the per-inlet dilution factors and refresh the Df column display."""
        self._dilution_factors = dict(factors)
        # Rebuild tree to reflect updated Df values
        if self._seq:
            result_inlets = self._result.inlets if self._result else None
            self._build_inlet_tree(self._seq, inlets=self._seq.inlets, result_inlets=result_inlets)

    # ------------------------------------------------------------------
    # Standard inlet exclusion
    # ------------------------------------------------------------------

    def _on_inlet_tree_context_menu(self, pos) -> None:
        """Right-click menu on standard/blank inlet rows for exclusion and
        force-include control."""
        item = self._inlet_tree.itemAt(pos)
        if item is None or not item.parent():
            return  # group header — ignore
        inlet_idx = item.data(0, Qt.UserRole)
        if inlet_idx is None or self._seq is None or self._result is None:
            return
        prep = self._seq.inlets[inlet_idx]
        seq_num = prep.seq_num

        # inlet_type lives on InletProcessingResult, not InletPrep
        result_ir = next((ir for ir in self._result.inlets if ir.seq_num == seq_num), None)
        if result_ir is None or result_ir.inlet_type not in ("standard", "blank"):
            return

        is_blank = result_ir.inlet_type == "blank"
        excl_dict  = self._excluded_blanks if is_blank else self._excluded_standards
        force_dict = self._force_included_blanks if is_blank else self._force_included_standards
        label = "blank" if is_blank else "standard"

        is_excl    = seq_num in excl_dict
        excl_entry = excl_dict.get(seq_num)  # None or set
        is_forced  = seq_num in force_dict
        force_entry = force_dict.get(seq_num)

        all_keys: List[str] = sorted(result_ir.isotopes.keys())

        menu = QMenu(self)
        if not is_excl:
            act_all = menu.addAction(f"⊘  Exclude from all isotopes")
            act_all.triggered.connect(
                lambda: self._set_inlet_exclusion(seq_num, None, is_blank)
            )
            if all_keys:
                iso_menu = menu.addMenu("⊘  Exclude for isotope…")
                for k in all_keys:
                    act_iso = iso_menu.addAction(k)
                    act_iso.triggered.connect(
                        lambda checked, _k=k: self._set_inlet_exclusion(seq_num, {_k}, is_blank)
                    )
        else:
            if excl_entry is None:
                menu.addAction("(Excluded from all isotopes)").setEnabled(False)
            else:
                menu.addAction(f"(Excluded for: {', '.join(sorted(excl_entry))})").setEnabled(False)
            menu.addSeparator()
            act_clear = menu.addAction("✓  Remove exclusion")
            act_clear.triggered.connect(
                lambda: self._clear_inlet_exclusion(seq_num, is_blank)
            )

        menu.addSeparator()
        if not is_forced:
            act_force_all = menu.addAction(f"⚑  Force-include ({label}, un-reject)")
            act_force_all.triggered.connect(
                lambda: self._set_inlet_force_include(seq_num, None, is_blank)
            )
            if all_keys:
                force_menu = menu.addMenu("⚑  Force-include for isotope…")
                for k in all_keys:
                    act_iso = force_menu.addAction(k)
                    act_iso.triggered.connect(
                        lambda checked, _k=k: self._set_inlet_force_include(seq_num, {_k}, is_blank)
                    )
        else:
            if force_entry is None:
                menu.addAction("(Force-included, all isotopes)").setEnabled(False)
            else:
                menu.addAction(f"(Force-included for: {', '.join(sorted(force_entry))})").setEnabled(False)
            act_clear_force = menu.addAction("✓  Remove force-include")
            act_clear_force.triggered.connect(
                lambda: self._clear_inlet_force_include(seq_num, is_blank)
            )

        menu.exec_(self._inlet_tree.viewport().mapToGlobal(pos))

    def _set_inlet_exclusion(self, seq_num: int, iso_keys: Optional[Set[str]], is_blank: bool) -> None:
        """Exclude seq_num from all isotopes (iso_keys=None) or a subset."""
        d = self._excluded_blanks if is_blank else self._excluded_standards
        d[seq_num] = iso_keys
        self._rerun_pipeline()

    def _clear_inlet_exclusion(self, seq_num: int, is_blank: bool) -> None:
        d = self._excluded_blanks if is_blank else self._excluded_standards
        d.pop(seq_num, None)
        self._rerun_pipeline()

    def _set_inlet_force_include(self, seq_num: int, iso_keys: Optional[Set[str]], is_blank: bool) -> None:
        """Force-include (un-reject) seq_num for all isotopes or a subset."""
        d = self._force_included_blanks if is_blank else self._force_included_standards
        d[seq_num] = iso_keys
        self._rerun_pipeline()

    def _clear_inlet_force_include(self, seq_num: int, is_blank: bool) -> None:
        d = self._force_included_blanks if is_blank else self._force_included_standards
        d.pop(seq_num, None)
        self._rerun_pipeline()

    # ------------------------------------------------------------------
    # Outlet override
    # ------------------------------------------------------------------

    def _reset_outliers(self) -> None:
        self._flag_overrides = {}
        self._rerun_pipeline(is_reset=True)

    def _rerun_pipeline(self, is_reset: bool = False) -> None:
        if self._seq is None:
            return
        cur_idx = self._current_inlet_idx()
        new_result = process_sequence(
            self._seq,
            config=self._config,
            flag_overrides=self._flag_overrides,
            fit_model_overrides=self._fit_model_overrides,
            repro_references=self._repro_references,
            multi_run_linearity=self._multi_run_linearity,
            aliquot_volumes=self._aliquot_volumes,
            excluded_standards=self._excluded_standards or None,
            excluded_blanks=self._excluded_blanks or None,
            force_included_standards=self._force_included_standards or None,
            force_included_blanks=self._force_included_blanks or None,
            blank_fit_overrides=self._blank_fit_overrides or None,
            drift_fit_overrides=self._drift_fit_overrides or None,
            linearity_fit_overrides=self._linearity_fit_overrides or None,
        )
        self._result = new_result
        self._build_inlet_tree(self._seq, inlets=self._seq.inlets, result_inlets=new_result.inlets)
        self._populate_summary_tab()
        self._populate_qc_tab()
        self._populate_gauge_tab()
        self._populate_sequence_tabs()
        if cur_idx >= 0:
            self._select_inlet_by_idx(cur_idx)
        if is_reset:
            self.outliers_reset.emit(new_result)
        else:
            self.result_changed.emit(new_result)

    def _set_flag_override(
        self,
        seq_num: int,
        device: str,
        action: str,
        pt_i: int,
        new_flag: bool,
    ) -> None:
        """Store a user-override for one signal point."""
        if seq_num not in self._flag_overrides:
            self._flag_overrides[seq_num] = {}

        if device not in self._flag_overrides[seq_num]:
            # Seed from the current auto flags so unchanged points are preserved
            row_idx = next(
                (i for i, ir in enumerate(self._result.inlets)
                 if ir.seq_num == seq_num),
                -1,
            )
            if row_idx >= 0:
                ir = self._result.inlets[row_idx]
                self._flag_overrides[seq_num][device] = {
                    act: list(bf.outlier_flags)
                    for act, bf in ir.block_fits.get(device, {}).items()
                }
            else:
                self._flag_overrides[seq_num][device] = {}

        flags = self._flag_overrides[seq_num][device].setdefault(action, [])
        while len(flags) <= pt_i:
            flags.append(False)
        flags[pt_i] = new_flag

    # ------------------------------------------------------------------
    # Selection / tab handlers
    # ------------------------------------------------------------------

    def _on_inlet_selected(self) -> None:
        row = self._current_inlet_idx()
        if row >= 0 and self._result is not None and self._seq is not None:
            self._refresh_inlet_view(row)

    def _on_device_tab_changed(self, _index: int) -> None:
        # If inlet signals popup is open, refresh it for the new device view
        row = self._current_inlet_idx()
        if row >= 0 and self._result is not None and self._seq is not None:
            if self._inlet_sig_popup and self._inlet_sig_popup.isVisible():
                self._draw_inlet_signals(row)

    def _refresh_inlet_view(self, row_idx: int) -> None:
        prep = self._seq.inlets[row_idx]
        ir   = self._result.inlets[row_idx]
        self._populate_signal_tables(prep, ir)
        self._populate_results_tab(ir)
        self._apply_isotope_filter()
        # Refresh whichever popup is currently open
        if self._inlet_sig_popup and self._inlet_sig_popup.isVisible():
            self._draw_inlet_signals(row_idx)
        if self._sms_popup and self._sms_popup.isVisible():
            self._draw_sms_raw_fit(row_idx)
        if self._qms_popup and self._qms_popup.isVisible():
            self._draw_qms_raw_fit(row_idx)
        if self._gauge_inlet_popup and self._gauge_inlet_popup.isVisible():
            self._draw_gauge_inlet(row_idx)

    # ------------------------------------------------------------------
    # Signal tables
    # ------------------------------------------------------------------

    def _populate_signal_tables(
        self,
        prep: InletPrep,
        ir: InletProcessingResult,
    ) -> None:
        t0 = prep.lv_time_start

        for idx, dev in enumerate(_DEVICES):
            tbl = self._signal_tables[dev]
            tbl.blockSignals(True)
            try:
                tbl.setRowCount(0)
                ms = prep.ms_by_device.get(dev)
                if ms is None or ms.resolved_path is None:
                    self._device_tabs.setTabText(idx, f"{dev} (—)")
                    continue

                signals   = ms.signals
                dev_fits  = ir.block_fits.get(dev, {})

                # Per-action point counter for indexing into outlier_flags
                action_counter: Dict[str, int] = {}

                tbl.setRowCount(len(signals))
                for r_idx, sig in enumerate(signals):
                    act  = sig.action
                    pt_i = action_counter.get(act, 0)
                    action_counter[act] = pt_i + 1

                    bfit  = dev_fits.get(act)
                    flags = bfit.outlier_flags if bfit else []
                    is_outlier = flags[pt_i] if pt_i < len(flags) else False

                    det_str = "Faraday" if sig.on_faraday else "Multiplier"

                    # Regular cells (read-only)
                    def _item(txt: str) -> QTableWidgetItem:
                        it = QTableWidgetItem(txt)
                        it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                        if is_outlier:
                            it.setBackground(_COL_OUTLIER_ROW)
                            it.setForeground(QColor("#B71C1C"))
                        return it

                    tbl.setItem(r_idx, 0, _item(str(r_idx + 1)))
                    tbl.item(r_idx, 0).setTextAlignment(Qt.AlignCenter)
                    tbl.setItem(r_idx, 1, _item(act))
                    t_item = _item(f"{sig.lv_time - t0:.1f}")
                    t_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    tbl.setItem(r_idx, 2, t_item)
                    s_item = _item(f"{sig.signal:.4e}")
                    s_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    tbl.setItem(r_idx, 3, s_item)
                    tbl.setItem(r_idx, 4, _item(det_str))
                    tbl.item(r_idx, 4).setTextAlignment(Qt.AlignCenter)

                    # Outlier checkbox (interactive)
                    chk = QTableWidgetItem()
                    chk.setFlags(
                        Qt.ItemIsEnabled | Qt.ItemIsSelectable
                        | Qt.ItemIsUserCheckable
                    )
                    chk.setCheckState(Qt.Checked if is_outlier else Qt.Unchecked)
                    chk.setData(Qt.UserRole, (act, pt_i))
                    chk.setTextAlignment(Qt.AlignCenter)
                    if is_outlier:
                        chk.setBackground(_COL_OUTLIER_ROW)
                    tbl.setItem(r_idx, _OUTLIER_COL, chk)

                self._device_tabs.setTabText(idx, f"{dev} ({len(signals)})")
            finally:
                tbl.blockSignals(False)

    def _on_signal_item_changed(self, item: QTableWidgetItem) -> None:
        """Called when any item in a signal table changes."""
        if item.column() != _OUTLIER_COL:
            return
        data = item.data(Qt.UserRole)
        if data is None:
            return
        action, pt_i = data

        # Identify device from tab
        dev_idx = self._device_tabs.currentIndex()
        if dev_idx < 0 or dev_idx >= len(_DEVICES):
            return
        dev = _DEVICES[dev_idx]

        row = self._current_inlet_idx()
        if row < 0 or self._result is None:
            return
        seq_num = self._result.inlets[row].seq_num

        new_flag = item.checkState() == Qt.Checked
        self._set_flag_override(seq_num, dev, action, pt_i, new_flag)
        self._rerun_pipeline()

    # ------------------------------------------------------------------
    # Results tab
    # ------------------------------------------------------------------

    def _populate_results_tab(self, ir: InletProcessingResult) -> None:
        tbl = self._results_tbl
        tbl.setRowCount(0)

        def _fmt(v: float) -> str:
            return f"{v:.4e}" if not math.isnan(v) else "—"

        rows_data = []
        for key in sorted(ir.isotopes.keys()):
            iso = ir.isotopes[key]
            device, isotope = key.split(":", 1)
            if not _is_reportable_action(isotope):
                continue
            mf = iso.meas_fit
            n_used = (mf.n_points - mf.n_outliers) if mf else 0
            meas_v = mf.value_at_ref if mf else float("nan")
            bg_v   = iso.bg_fit.value_at_ref if iso.bg_fit else float("nan")
            # Per-isotope certified reference amount (standard inlets only)
            if ir.inlet_type == "standard":
                ref_for_iso = (
                    ir.reference_amounts.get(key)
                    or ir.reference_amounts.get(isotope)
                    or ir.reference_amount
                )
            else:
                ref_for_iso = float("nan")
            bg_used_str = getattr(iso, "bg_used", "original")
            rows_data.append((
                device, isotope, iso.signal_fit_model, n_used,
                meas_v, bg_v,
                iso.net_signal, iso.blank_net, iso.blank_corrected,
                ref_for_iso, iso.ccSTP, iso.ccSTP_unc, bg_used_str
            ))

        tbl.setRowCount(len(rows_data))
        for r, (dev, iso, fit_model, n_used, meas, bg, net, blank, bc, ref_iso, ccstp, ccstp_u, bg_used_val) in \
                enumerate(rows_data):
            items = [
                QTableWidgetItem(dev),
                QTableWidgetItem(iso),
                QTableWidgetItem("Average" if fit_model == "block" else fit_model),
                QTableWidgetItem(str(n_used)),
                QTableWidgetItem(_fmt(meas)),
                QTableWidgetItem(_fmt(bg)),
                QTableWidgetItem(_fmt(net)),
                QTableWidgetItem(_fmt(blank)),
                QTableWidgetItem(_fmt(bc)),
                QTableWidgetItem(_fmt(ref_iso)),
                QTableWidgetItem(_fmt(ccstp)),
                QTableWidgetItem(_fmt(ccstp_u)),
                QTableWidgetItem(bg_used_val),
            ]
            items[0].setTextAlignment(Qt.AlignCenter)
            items[1].setTextAlignment(Qt.AlignCenter)
            items[2].setTextAlignment(Qt.AlignCenter)
            items[3].setTextAlignment(Qt.AlignCenter)
            for i in range(4, 12):
                items[i].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            items[12].setTextAlignment(Qt.AlignCenter)
            for c, it in enumerate(items):
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                tbl.setItem(r, c, it)

    # ------------------------------------------------------------------
    # Summary tab
    # ------------------------------------------------------------------

    def _populate_summary_tab(self) -> None:
        tbl = self._summary_tbl
        tbl.setRowCount(0)
        if self._result is None:
            return

        pr = self._result
        all_keys = sorted(set(pr.blank_means) | set(pr.sensitivities))
        tbl.setRowCount(len(all_keys))

        def _fmt(v: float) -> str:
            return f"{v:.4e}" if not math.isnan(v) else "—"

        for r, key in enumerate(all_keys):
            dev, iso = key.split(":", 1) if ":" in key else (key, "")
            items = [
                QTableWidgetItem(dev),
                QTableWidgetItem(iso),
                QTableWidgetItem(_fmt(pr.blank_means.get(key, float("nan")))),
                QTableWidgetItem(_fmt(pr.blank_uncs.get(key, float("nan")))),
                QTableWidgetItem(_fmt(pr.sensitivities.get(key, float("nan")))),
                QTableWidgetItem(_fmt(pr.sensitivity_uncs.get(key, float("nan")))),
            ]
            items[0].setTextAlignment(Qt.AlignCenter)
            items[1].setTextAlignment(Qt.AlignCenter)
            for i in range(2, 6):
                items[i].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            for c, it in enumerate(items):
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                tbl.setItem(r, c, it)

    # ------------------------------------------------------------------
    # QC tab
    # ------------------------------------------------------------------

    def _populate_qc_tab(self) -> None:
        if self._result is None:
            self._qc_browser.setHtml("<i>No results yet.</i>")
            return

        pr = self._result
        cfg = self._config

        def _r2(v: float) -> str:
            return f"{v:.4f}" if not math.isnan(v) else "—"

        _R2_WARN  = 0.30   # below this → red
        _R2_MARGINAL = 0.65  # below this → amber

        def _r2_cell(v: float, fit_type: str = "") -> str:
            """Return a <td> with R² value, coloured red/amber when the fit is poor."""
            if math.isnan(v):
                return '<td align="right">—</td>'
            txt = f"{v:.4f}"
            # Only colour regression fits; skip mean/none/akima (NaN R² anyway)
            is_regression = any(
                m in fit_type
                for m in ("linear", "quadratic", "cubic", "exponential",
                          "auto→linear", "auto→quadratic", "auto→cubic")
            )
            if is_regression and v < _R2_WARN:
                return f'<td align="right" style="background:#FFCDD2; color:#B71C1C;">{txt}</td>'
            if is_regression and v < _R2_MARGINAL:
                return f'<td align="right" style="background:#FFF9C4; color:#795548;">{txt}</td>'
            return f'<td align="right">{txt}</td>'

        def _e(v: float) -> str:
            return f"{v:.3e}" if not math.isnan(v) else "—"

        def _pct(v: float) -> str:
            return f"{v * 100:.1f} %" if not math.isnan(v) else "—"

        lines = ['<html><body style="font-family:monospace; font-size:11px;">']

        # ── Run statistics ────────────────────────────────────────────
        lines.append('<h3 style="margin:4px 0 2px;">Run Statistics</h3>')
        lines.append('<table cellspacing="0" cellpadding="2">')
        lines.append(f'<tr><td>Blanks&nbsp;</td><td><b>{pr.n_blanks}</b></td></tr>')
        lines.append(f'<tr><td>Standards&nbsp;</td><td><b>{pr.n_standards}</b></td></tr>')
        lines.append(f'<tr><td>Samples&nbsp;</td><td><b>{pr.n_samples}</b></td></tr>')
        lines.append('</table>')

        # ── Signal fitting ────────────────────────────────────────────
        lines.append('<h3 style="margin:8px 0 2px;">Signal Fitting (per-inlet t = 0 extrapolation)</h3>')
        lines.append(f'<p style="margin:0 0 4px;">Config: <b>{cfg.signal_fit_model}</b></p>')
        model_counts: Dict[str, int] = {}
        for ir in pr.inlets:
            for iso in ir.isotopes.values():
                m = iso.signal_fit_model
                model_counts[m] = model_counts.get(m, 0) + 1
        lines.append('<table border="0" cellspacing="0" cellpadding="2" width="100%">')
        lines.append('<tr style="background:#E8EAF6;">'
                     '<th align="left">Model used</th>'
                     '<th align="right">Inlet-isotopes</th>'
                     '</tr>')
        for model, count in sorted(model_counts.items(), key=lambda x: -x[1]):
            lines.append(f'<tr><td>{model}</td><td align="right">{count}</td></tr>')
        lines.append('</table>')

        # ── Blank interpolation ───────────────────────────────────────
        lines.append('<h3 style="margin:8px 0 2px;">Blank Interpolation</h3>')
        lines.append(f'<p style="margin:0 0 4px;">Config: <b>{cfg.blank_interpolation}</b></p>')
        if pr.blank_fits:
            lines.append('<table border="0" cellspacing="0" cellpadding="2" width="100%">')
            lines.append('<tr style="background:#E8EAF6;">'
                         '<th align="left">Isotope key</th>'
                         '<th align="left">Fit type</th>'
                         '<th align="right">N blanks</th>'
                         '<th align="right">R²</th>'
                         '<th align="right">Mean (A)</th>'
                         '<th align="right">± (A)</th>'
                         '</tr>')
            for key in sorted(pr.blank_fits):
                bf = pr.blank_fits[key]
                mean_b = pr.blank_means.get(key, float("nan"))
                unc_b  = pr.blank_uncs.get(key, float("nan"))
                n_tot  = len(bf.blank_times)
                n_out  = sum(bf.outlier_mask) if bf.outlier_mask else 0
                n_lbl  = f"{n_tot - n_out}/{n_tot}" if n_out else str(n_tot)
                lines.append(
                    f'<tr><td>{key}</td><td>{bf.fit_type}</td>'
                    f'<td align="right">{n_lbl}</td>'
                    + _r2_cell(bf.r_squared, bf.fit_type) +
                    f'<td align="right">{_e(mean_b)}</td>'
                    f'<td align="right">{_e(unc_b)}</td></tr>'
                )
            lines.append('</table>')
        else:
            lines.append('<p style="color:#888; margin:0;">No blank fit data.</p>')

        # ── Drift / Sensitivity ───────────────────────────────────────
        lines.append('<h3 style="margin:8px 0 2px;">Drift / Sensitivity</h3>')
        lines.append(f'<p style="margin:0 0 4px;">Config: <b>{cfg.drift_correction}</b> '
                     f'(min standards: {cfg.min_std_for_drift})</p>')
        lines.append('<table border="0" cellspacing="0" cellpadding="2" width="100%">')
        lines.append('<tr style="background:#E8EAF6;">'
                     '<th align="left">Isotope key</th>'
                     '<th align="left">Fit type</th>'
                     '<th align="right">N stds</th>'
                     '<th align="right">R²</th>'
                     '<th align="right">Mean S (A/ccSTP)</th>'
                     '<th align="right">%RSD</th>'
                     '</tr>')
        for key in sorted(pr.sensitivities):
            mean_s = pr.sensitivities[key]
            unc_s  = pr.sensitivity_uncs.get(key, float("nan"))
            rsd    = abs(unc_s / mean_s) if mean_s and not math.isnan(mean_s) else float("nan")
            df     = pr.drift_fits.get(key)
            fit_type = df.fit_type if df else "mean"
            r2_s   = df.r_squared if df else float("nan")
            n_stds = len(df.std_times) if df else sum(
                1 for ir in pr.inlets
                if ir.inlet_type == "standard" and key in ir.isotopes
                and not math.isnan(ir.isotopes[key].inlet_sensitivity)
            )
            df_obj  = pr.drift_fits.get(key)
            n_out_d = sum(df_obj.outlier_mask) if df_obj and df_obj.outlier_mask else 0
            n_lbl_d = f"{n_stds - n_out_d}/{n_stds}" if n_out_d else str(n_stds)
            lines.append(
                f'<tr><td>{key}</td><td>{fit_type}</td>'
                f'<td align="right">{n_lbl_d}</td>'
                + _r2_cell(r2_s, fit_type) +
                f'<td align="right">{_e(mean_s)}</td>'
                f'<td align="right">{_pct(rsd)}</td></tr>'
            )
        lines.append('</table>')

        # ── Linearity correction ──────────────────────────────────────
        lines.append('<h3 style="margin:8px 0 2px;">Linearity Correction</h3>')
        lines.append(f'<p style="margin:0 0 4px;">Config: <b>{cfg.linearity_correction}</b></p>')
        if pr.linearity_fits:
            lines.append('<table border="0" cellspacing="0" cellpadding="2" width="100%">')
            lines.append('<tr style="background:#E8EAF6;">'
                         '<th align="left">Isotope key</th>'
                         '<th align="left">Fit type</th>'
                         '<th align="right">N stds</th>'
                         '<th align="right">R²</th>'
                         '</tr>')
            for key in sorted(pr.linearity_fits):
                lf = pr.linearity_fits[key]
                lines.append(
                    f'<tr><td>{key}</td><td>{lf.fit_type}</td>'
                    f'<td align="right">{len(lf.signal_levels)}</td>'
                    + _r2_cell(lf.r_squared, lf.fit_type) + '</tr>'
                )
            lines.append('</table>')
        else:
            lines.append('<p style="color:#888; margin:0;">No linearity correction applied.</p>')

        # ── Warnings ─────────────────────────────────────────────────
        warnings = []
        if pr.n_blanks == 0:
            warnings.append("No blank inlets — blank correction skipped.")
        if pr.n_standards == 0:
            warnings.append("No standard inlets — sensitivity cannot be computed.")
        for key, mean_s in pr.sensitivities.items():
            unc_s = pr.sensitivity_uncs.get(key, float("nan"))
            if not math.isnan(mean_s) and mean_s > 0 and not math.isnan(unc_s):
                rsd = abs(unc_s / mean_s)
                if rsd > 0.05:
                    warnings.append(
                        f"{key}: sensitivity %RSD = {rsd * 100:.1f} % (> 5 %)"
                    )
        block_count = sum(
            1 for ir in pr.inlets
            for iso in ir.isotopes.values()
            if iso.signal_fit_model == "block"
        )
        if block_count > 0:
            warnings.append(
                f"{block_count} inlet-isotope(s) used block-level fit "
                f"(signal fitter fallback — check SMS/QMS file paths)."
            )
        # R² quality warnings — only for regression fits (not mean/akima/none)
        _regression_types = {"linear", "quadratic", "cubic", "exponential"}

        def _is_regression(ft: str) -> bool:
            return any(rt in ft for rt in _regression_types)

        for key in sorted(pr.blank_fits):
            bf = pr.blank_fits[key]
            if _is_regression(bf.fit_type) and not math.isnan(bf.r_squared) and bf.r_squared < _R2_WARN:
                warnings.append(
                    f"Blank fit — {key}: R²={bf.r_squared:.3f} &lt; {_R2_WARN} "
                    f"({bf.fit_type}). Consider switching to Auto or Mean."
                )
        for key in sorted(pr.drift_fits):
            df = pr.drift_fits.get(key)
            if df and _is_regression(df.fit_type) and not math.isnan(df.r_squared) and df.r_squared < _R2_WARN:
                warnings.append(
                    f"Drift fit — {key}: R²={df.r_squared:.3f} &lt; {_R2_WARN} "
                    f"({df.fit_type}). Sensitivity shows no clear trend; consider None or Auto."
                )
        for key in sorted(pr.linearity_fits):
            lf = pr.linearity_fits[key]
            if _is_regression(lf.fit_type) and not math.isnan(lf.r_squared) and lf.r_squared < _R2_WARN:
                warnings.append(
                    f"Linearity fit — {key}: R²={lf.r_squared:.3f} &lt; {_R2_WARN} "
                    f"({lf.fit_type}). Correction may degrade results."
                )

        if warnings:
            lines.append('<h3 style="margin:8px 0 2px; color:#B71C1C;">Warnings</h3>')
            lines.append('<ul style="margin:0; padding-left:18px;">')
            for w in warnings:
                lines.append(f'<li style="color:#B71C1C;">{w}</li>')
            lines.append('</ul>')
        else:
            lines.append(
                '<p style="color:#2E7D32; margin:8px 0 0;">'
                '&#10003; No warnings.</p>'
            )

        # Gauge QC warnings — appended here when gauge_qc_flags is on
        gs = getattr(pr, "gauge_summary", None)
        if gs is not None:
            flagged = [
                ig for ig in gs.inlets
                if any(ig.qc_flags.get(ch, False) for ch in PRIMARY_CHANNELS)
            ]
            if flagged:
                lines.append('<h3 style="margin:8px 0 2px; color:#E65100;">Gauge Pressure Flags</h3>')
                lines.append('<ul style="margin:0; padding-left:18px;">')
                for ig in flagged:
                    chlist = ", ".join(
                        f"{ch}={ig.channels[ch].mean:.3g}"
                        for ch in PRIMARY_CHANNELS
                        if ig.qc_flags.get(ch, False) and ch in ig.channels
                    )
                    lines.append(
                        f'<li style="color:#E65100;">Inlet {ig.seq_num} '
                        f'<b>{ig.description}</b>: anomalous SRG ({chlist}) '
                        f'— possible leak or outgassing</li>'
                    )
                lines.append('</ul>')

        lines.append('</body></html>')
        self._qc_browser.setHtml("\n".join(lines))

    # ------------------------------------------------------------------
    # Gauge tab
    # ------------------------------------------------------------------

    def _populate_gauge_tab(self) -> None:
        """Fill the Gauge Summary table, SRG time-series, and inlet detail."""
        self._gauge_tbl.setRowCount(0)
        self._srg_fig.clear()
        self._srg_canvas.draw()
        self._detail_fig.clear()
        self._detail_canvas.draw()

        gs: Optional[GaugeSequenceSummary] = getattr(
            self._result, "gauge_summary", None
        )
        if gs is None or not gs.inlets:
            self._lbl_gauge_filter_count.setText("")
            return

        available_primary = [c for c in PRIMARY_CHANNELS if c in gs.channels_available]
        available_secondary = [c for c in SECONDARY_CHANNELS if c in gs.channels_available]
        all_avail = available_primary + available_secondary

        # gauge_conc / gauge_conc_per_g live on the main InletProcessingResult
        # (populated by _compute_gauge_concentrations), not on the gauge-specific
        # InletGaugeSummary -- cross-reference by seq_num, same pattern used
        # elsewhere in this file (e.g. the inlet-detail panel).
        _elements = ["He", "Ne", "Ar"]
        _conc_by_seq: Dict[int, InletProcessingResult] = {
            ir.seq_num: ir for ir in (self._result.inlets or [])
        }
        has_conc = any(
            (ir.gauge_conc for ir in _conc_by_seq.values())
        )
        has_conc_per_g = any(
            (ir.gauge_conc_per_g for ir in _conc_by_seq.values())
        )

        # "Samples only" filter
        samples_only = self._chk_gauge_samples_only.isChecked()
        visible_inlets = (
            [ig for ig in gs.inlets if ig.inlet_type == "sample"]
            if samples_only else gs.inlets
        )
        self._lbl_gauge_filter_count.setText(
            f"{len(visible_inlets)} of {len(gs.inlets)}" if samples_only else ""
        )

        # ── Summary table ────────────────────────────────────────────────
        _conc_cols = _elements if has_conc else []
        _conc_pg_cols = _elements if has_conc_per_g else []
        _col_names = (
            ["#", "Inlet", "Type"]
            + [f"{el} (ccSTP)" for el in _conc_cols]
            + [f"{el} (ccSTP/g)" for el in _conc_pg_cols]
            + all_avail
        )
        self._gauge_tbl.setColumnCount(len(_col_names))
        self._gauge_tbl.setHorizontalHeaderLabels(_col_names)
        self._gauge_tbl.setRowCount(len(visible_inlets))

        _type_colors = {
            "blank":    QColor("#E3F2FD"),
            "standard": QColor("#F3E5F5"),
            "sample":   QColor("#FFFFFF"),
        }
        _flag_color = QColor("#FFCCBC")

        for row, ig in enumerate(visible_inlets):
            row_flagged = any(ig.qc_flags.get(ch, False) for ch in PRIMARY_CHANNELS)
            bg = _flag_color if row_flagged else _type_colors.get(ig.inlet_type, QColor("#FFFFFF"))

            def _cell(text: str) -> QTableWidgetItem:
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                item.setBackground(bg)
                return item

            self._gauge_tbl.setItem(row, 0, _cell(str(ig.seq_num)))
            self._gauge_tbl.setItem(row, 1, _cell(ig.description))
            self._gauge_tbl.setItem(row, 2, _cell(ig.inlet_type))

            col_idx = 3
            ir = _conc_by_seq.get(ig.seq_num)
            for el in _conc_cols:
                v = ir.gauge_conc.get(el) if ir else None
                u = ir.gauge_conc_unc.get(el) if ir else None
                txt = "—" if v is None else (f"{v:.4g} ± {u:.2g}" if u is not None else f"{v:.4g}")
                self._gauge_tbl.setItem(row, col_idx, _cell(txt))
                col_idx += 1
            for el in _conc_pg_cols:
                v = ir.gauge_conc_per_g.get(el) if ir else None
                u = ir.gauge_conc_per_g_unc.get(el) if ir else None
                txt = "—" if v is None else (f"{v:.4g} ± {u:.2g}" if u is not None else f"{v:.4g}")
                self._gauge_tbl.setItem(row, col_idx, _cell(txt))
                col_idx += 1

            for ch in all_avail:
                cs = ig.channels.get(ch)
                if cs is None or math.isnan(cs.mean):
                    self._gauge_tbl.setItem(row, col_idx, _cell("—"))
                else:
                    flagged = ig.qc_flags.get(ch, False)
                    txt = f"{cs.mean:.3g} ± {cs.sigma:.2g}"
                    item = _cell(txt)
                    if flagged:
                        item.setForeground(QColor("#B71C1C"))
                    self._gauge_tbl.setItem(row, col_idx, item)
                col_idx += 1

        # Column widths: "Inlet" (col 1) stretches, everything else sizes to
        # content -- column set is dynamic (conc/per-g columns only appear
        # when that data exists) so this is re-applied on every populate.
        self._gauge_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for _c in range(len(_col_names)):
            if _c != 1:
                self._gauge_tbl.horizontalHeader().setSectionResizeMode(_c, QHeaderView.ResizeToContents)

        # ── SRG / Total-Pressure Time Series (GaugeSignal worksheet) ────────
        # Include BaratronInlet (Total Pressure) + SRGHeNe + SRGAr if available
        _gs_plot_channels = (
            [c for c in ["BaratronInlet"] + list(PRIMARY_CHANNELS) if c in gs.channels_available]
        )
        if not _gs_plot_channels:
            self._srg_canvas.draw()
        else:
            self._srg_fig.clear()
            n_ch = len(_gs_plot_channels)
            axes = self._srg_fig.subplots(n_ch, 1, sharex=True)
            if n_ch == 1:
                axes = [axes]

            _inlet_colors = {"blank": "#1565C0", "standard": "#6A1B9A", "sample": "#2E7D32"}

            for ax, ch in zip(axes, _gs_plot_channels):
                med = gs.run_medians.get(ch, float("nan"))

                # Layer 1: full-sequence raw continuous data (light grey)
                if gs.raw_t and ch in gs.raw_data:
                    raw_y = gs.raw_data[ch]
                    valid = [(t, y) for t, y in zip(gs.raw_t, raw_y) if not math.isnan(y)]
                    if valid:
                        vt, vy = zip(*valid)
                        ax.scatter(vt, vy, s=3, color="#BDBDBD", alpha=0.5, zorder=1)

                # Layer 2: per-inlet σ-clipped points and means
                for ig in gs.inlets:
                    cs = ig.channels.get(ch)
                    if cs is None:
                        continue
                    col = _inlet_colors.get(ig.inlet_type, "#555")
                    if cs.t_vals:
                        ax.scatter(cs.t_vals, cs.y_vals, s=8, color=col, alpha=0.75, zorder=3)
                    if cs.t_out:
                        ax.scatter(cs.t_out, cs.y_out, s=14, color="#E53935",
                                   marker="x", linewidths=1, zorder=4)
                    if not math.isnan(cs.mean):
                        t0 = cs.t_vals[0] if cs.t_vals else ig.t_start - gs.inlets[0].t_start
                        t1 = cs.t_vals[-1] if cs.t_vals else ig.t_end - gs.inlets[0].t_start
                        ax.hlines(cs.mean, t0, t1, color=col, linewidth=2.0, zorder=5)
                        if ig.qc_flags.get(ch, False):
                            ax.hlines(cs.mean, t0, t1, color="#E53935",
                                      linewidth=2, linestyle="--", zorder=6)

                # Layer 3: run-median reference line
                if not math.isnan(med):
                    ax.axhline(med, color="#9E9E9E", linewidth=0.8, linestyle=":", zorder=2)

                # Layer 4: polynomial fit through per-inlet means (GaugeSignal fit)
                fit = gs.channel_fits.get(ch)
                if fit and fit.coeffs and gs.raw_t:
                    t_lo, t_hi = min(gs.raw_t), max(gs.raw_t)
                    n_pts = max(200, len(gs.raw_t))
                    step = (t_hi - t_lo) / (n_pts - 1) if n_pts > 1 else 1.0
                    fit_t = [t_lo + i * step for i in range(n_pts)]

                    def _eval_poly(xs, coeffs=fit.coeffs):
                        result = []
                        for x in xs:
                            v = 0.0
                            for c in coeffs:
                                v = v * x + c
                            result.append(v)
                        return result

                    fit_y = _eval_poly(fit_t)
                    ax.plot(fit_t, fit_y, color="#E65100", linewidth=1.5,
                            linestyle="-", zorder=7,
                            label=f"poly{fit.degree} R²={fit.r_squared:.3f}")
                    ax.legend(fontsize=7, loc="upper right")

                _ch_label = {"BaratronInlet": "Total P. (Baratron)"}.get(ch, ch)
                ax.set_ylabel(_ch_label, fontsize=8)
                ax.tick_params(labelsize=7)
                ax.grid(True, alpha=0.3)

            axes[-1].set_xlabel("Time from sequence start (s)", fontsize=8)
            self._srg_fig.suptitle(
                "Gauge Signal — full sequence (grey=raw, coloured=σ-clipped, orange=fit)",
                fontsize=8, y=0.99,
            )
            self._srg_canvas.draw()

        # Pre-render the gauge inlet detail so the popup is ready immediately
        row = self._current_inlet_idx()
        seed_ig = None
        if row >= 0 and self._seq is not None and row < len(self._seq.inlets):
            seed_seq_num = self._seq.inlets[row].seq_num
            seed_ig = next((i for i in gs.inlets if i.seq_num == seed_seq_num), None)
        if seed_ig is None and gs.inlets:
            seed_ig = gs.inlets[0]
        if seed_ig is not None:
            self._draw_inlet_detail(seed_ig, all_avail)

    def _populate_unc_prop_tab(self) -> None:
        """
        Per-inlet uncertainty-propagation trace with jump detection: groups
        the captured [UNC] lines by inlet (rows with no "inletN:" prefix are
        sequence-level, grouped under "(sequence)"), and flags the largest
        relative-uncertainty jump between consecutive stages within each
        group. Mirrors NgamImport.tsx's Unc Prop tab; parse_unc_trace_lines()
        is the same shared-core helper the web backend's API endpoint uses,
        so both sides interpret the trace identically.
        """
        self._unc_tree.clear()

        rows = parse_unc_trace_lines(self._unc_trace) if self._unc_trace else []

        # Isotope filter combo (rebuild options, preserve selection if still valid)
        prev_selection = self._combo_unc_isotope.currentData()
        self._combo_unc_isotope.blockSignals(True)
        self._combo_unc_isotope.clear()
        self._combo_unc_isotope.addItem("All", None)
        for iso in sorted({r["isotope_key"] for r in rows}):
            self._combo_unc_isotope.addItem(iso, iso)
        idx = self._combo_unc_isotope.findData(prev_selection)
        self._combo_unc_isotope.setCurrentIndex(idx if idx >= 0 else 0)
        self._combo_unc_isotope.blockSignals(False)

        iso_filter = self._combo_unc_isotope.currentData()
        if iso_filter:
            rows = [r for r in rows if r["isotope_key"] == iso_filter]

        if not rows:
            self._unc_tree.setVisible(False)
            self._lbl_unc_empty.setVisible(True)
            return
        self._unc_tree.setVisible(True)
        self._lbl_unc_empty.setVisible(False)

        by_inlet: Dict[str, list] = {}
        for r in rows:
            by_inlet.setdefault(r["inlet"] or "(sequence)", []).append(r)

        def _sort_key(k: str):
            return (1, 0) if k == "(sequence)" else (0, int(k))

        _jump_color = QColor("#F59E0B")
        _dim_color = QColor("#78909C")

        for inlet_key in sorted(by_inlet.keys(), key=_sort_key):
            group_rows = by_inlet[inlet_key]
            grp_label = "Sequence-level" if inlet_key == "(sequence)" else f"Inlet {inlet_key}"
            grp_item = QTreeWidgetItem(self._unc_tree)
            grp_item.setText(0, grp_label)
            grp_item.setExpanded(True)
            font = grp_item.font(0)
            font.setBold(True)
            grp_item.setFont(0, font)
            grp_item.setFlags(Qt.ItemIsEnabled)

            prev_rel = None
            max_jump = None
            for r in group_rows:
                jump = (r["rel"] - prev_rel) if prev_rel is not None else None
                if jump is not None and (max_jump is None or jump > max_jump):
                    max_jump = jump
                prev_rel = r["rel"]

                leaf = QTreeWidgetItem(grp_item)
                leaf.setText(0, r["stage"])
                leaf.setText(1, r["isotope_key"])
                leaf.setText(2, f"{r['value']:.6g}")
                leaf.setText(3, f"{r['unc']:.6g}")
                leaf.setText(4, f"{r['rel']:.2f}%")
                leaf.setText(5, f"{jump:+.2f}" if jump is not None else "—")
                for c in (2, 3, 4, 5):
                    leaf.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
                leaf.setForeground(1, _dim_color)
                if jump is not None and jump > 1.0:
                    leaf.setForeground(5, _jump_color)
                    font5 = leaf.font(5)
                    font5.setBold(True)
                    leaf.setFont(5, font5)
                else:
                    leaf.setForeground(5, _dim_color)

            if max_jump is not None:
                note = QTreeWidgetItem(grp_item)
                note.setText(0, f"Biggest jump: {max_jump:+.2f} pts")
                note.setFirstColumnSpanned(True)
                note.setForeground(0, _jump_color)
                note.setFlags(Qt.ItemIsEnabled)

    def _refresh_gauge_inlet(self, seq_num: int) -> None:
        gs: Optional[GaugeSequenceSummary] = getattr(
            self._result, "gauge_summary", None
        )
        if gs is None:
            return
        ig = next((i for i in gs.inlets if i.seq_num == seq_num), None)
        if ig is None:
            return
        all_avail = (
            [c for c in PRIMARY_CHANNELS if c in gs.channels_available]
            + [c for c in SECONDARY_CHANNELS if c in gs.channels_available]
        )
        self._draw_inlet_detail(ig, all_avail)

    def _draw_inlet_detail(
        self, ig: InletGaugeSummary, channels: List[str]
    ) -> None:
        self._detail_fig.clear()
        avail = [c for c in channels if c in ig.channels]
        if not avail:
            self._detail_canvas.draw()
            return
        n = len(avail)
        cols = min(n, 4)
        rows = math.ceil(n / cols)
        axes = self._detail_fig.subplots(rows, cols)
        # Flatten axes grid
        if n == 1:
            axes = [axes]
        elif rows == 1:
            axes = list(axes)
        else:
            axes = [ax for row in axes for ax in row]

        for ax, ch in zip(axes, avail):
            cs = ig.channels[ch]
            if cs.t_vals:
                ax.scatter(cs.t_vals, cs.y_vals, s=8, color="#1565C0",
                           alpha=0.7, label=f"n={cs.n_points}", zorder=3)
            if cs.t_out:
                ax.scatter(cs.t_out, cs.y_out, s=16, color="#E53935",
                           marker="x", linewidths=1.2, label=f"out={cs.n_outliers}", zorder=4)
            if not math.isnan(cs.mean):
                all_t = cs.t_vals + cs.t_out
                if all_t:
                    ax.hlines(cs.mean, min(all_t), max(all_t),
                              color="#E65100", linewidth=1.5, zorder=5)
                    ax.axhspan(cs.mean - cs.sigma, cs.mean + cs.sigma,
                               alpha=0.12, color="#E65100")
            ax.set_title(ch, fontsize=8)
            ax.tick_params(labelsize=7)
            ax.grid(True, alpha=0.3)
            if cs.n_points > 0:
                ax.legend(fontsize=7, loc="upper right")

        # Hide unused axes
        for ax in axes[n:]:
            ax.set_visible(False)

        self._detail_fig.suptitle(
            f"Inlet {ig.seq_num}  —  {ig.description}", fontsize=9, y=0.98
        )
        self._detail_canvas.draw()
