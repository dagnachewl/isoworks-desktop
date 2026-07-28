"""
ngam_inlet_signals_widget.py
============================
Detached popup widget that shows every signal action for one inlet
as its own matplotlib panel (2-column scrollable grid, one tab per device).

Usage
-----
    w = InletSignalsWidget()
    w.load(prep, ir)
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout, QScrollArea, QTabWidget,
    QLabel, QSizePolicy,
)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.ticker as mticker

from plot_utils import attach_hover_tooltip

# Devices in display order
_DEVICES = ("SMS", "QMSNe", "QMSAr", "QMSKrXe")

_ACTION_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
    "#bcbd22", "#17becf",
]


# ---------------------------------------------------------------------------
# Per-action panel
# ---------------------------------------------------------------------------

class ActionSignalPanel(QWidget):
    """
    Small matplotlib panel for a single action's signal vs time.
    Shows good points (filled), outliers (red ×), and the linear fit overlay.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(0)

        self._fig = Figure(figsize=(4, 2.6), constrained_layout=True)
        self._ax  = self._fig.add_subplot(111)
        self._canvas = FigureCanvas(self._fig)
        self._canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lay.addWidget(self._canvas)

    def load(
        self,
        action:        str,
        times_rel:     List[float],
        signals:       List[float],
        outlier_flags: List[bool],
        bfit,                         # BlockFit | None
        color:         str,
        t_ref_rel:     Optional[float] = None,
    ) -> None:
        ax = self._ax
        ax.clear()

        if not times_rel:
            ax.text(0.5, 0.5, "No data", ha="center", va="center",
                    transform=ax.transAxes, color="gray", fontsize=8)
            ax.set_axis_off()
            self._canvas.draw()
            return

        eff_flags = (list(outlier_flags) + [False] * len(times_rel))[:len(times_rel)]

        good_t = [t for t, f in zip(times_rel, eff_flags) if not f]
        good_s = [s for s, f in zip(signals,   eff_flags) if not f]
        bad_t  = [t for t, f in zip(times_rel, eff_flags) if f]
        bad_s  = [s for s, f in zip(signals,   eff_flags) if f]

        ms = 4 if len(times_rel) < 60 else 2

        sc_good = ax.scatter(good_t, good_s, color=color, s=ms ** 2, zorder=3)
        ln_bad = None
        if bad_t:
            ln_bad, = ax.plot(bad_t, bad_s,
                    marker="x", linestyle="none", color="#D32F2F",
                    markersize=7, markeredgewidth=2, label="outlier")

        # Linear fit overlay
        if bfit is not None and not math.isnan(bfit.value_at_ref) and not math.isnan(bfit.slope):
            x_lo, x_hi = min(times_rel), max(times_rel)
            x_ref = t_ref_rel if t_ref_rel is not None else 0.0
            y_lo  = bfit.value_at_ref + bfit.slope * (x_lo - x_ref)
            y_hi  = bfit.value_at_ref + bfit.slope * (x_hi - x_ref)
            ax.plot([x_lo, x_hi], [y_lo, y_hi],
                    linestyle="--", color=color, linewidth=1.4, alpha=0.85)

        # Dynamic y-range with 15 % padding
        all_s = good_s + bad_s
        if bfit is not None and not math.isnan(bfit.value_at_ref):
            x_lo, x_hi = min(times_rel), max(times_rel)
            x_ref = t_ref_rel if t_ref_rel is not None else 0.0
            all_s.append(bfit.value_at_ref + bfit.slope * (x_lo - x_ref))
            all_s.append(bfit.value_at_ref + bfit.slope * (x_hi - x_ref))
        if all_s:
            y_lo_d, y_hi_d = min(all_s), max(all_s)
            span = y_hi_d - y_lo_d if y_hi_d != y_lo_d else abs(y_hi_d) * 0.1 or 1e-12
            ax.set_ylim(y_lo_d - 0.15 * span, y_hi_d + 0.15 * span)

        ax.set_title(action, fontsize=8, pad=2)
        ax.set_xlabel("Time (s)", fontsize=7)
        ax.set_ylabel("Signal (A)", fontsize=7)
        ax.tick_params(labelsize=6)
        try:
            ax.yaxis.set_major_formatter(mticker.ScalarFormatter(useMathText=True))
            ax.ticklabel_format(style="sci", axis="y", scilimits=(0, 0))
        except Exception:
            pass
        if bad_t:
            ax.legend(fontsize=6, loc="best")

        entries = [(sc_good, lambda i, x, y, a=action:
                    f"{a}\nt = {x:.1f} s\n{y:.4e} A")]
        if ln_bad is not None:
            entries.append((ln_bad, lambda i, x, y, a=action:
                            f"OUTLIER — {a}\nt = {x:.1f} s\n{y:.4e} A"))
        attach_hover_tooltip(self._canvas, ax, entries)

        self._canvas.draw()


# ---------------------------------------------------------------------------
# Per-device page: 2-column scrollable grid of ActionSignalPanel
# ---------------------------------------------------------------------------

class _DevicePage(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        self._container = QWidget()
        self._grid = QGridLayout(self._container)
        self._grid.setContentsMargins(2, 2, 2, 2)
        self._grid.setSpacing(6)

        scroll.setWidget(self._container)
        lay.addWidget(scroll)

        self._panels: Dict[str, ActionSignalPanel] = {}

    def _panel_is_alive(self, panel: ActionSignalPanel) -> bool:
        """Return True if the C++ object backing *panel* still exists."""
        try:
            import sip
            return not sip.isdeleted(panel)
        except (ImportError, AttributeError):
            # Fallback: sip unavailable — assume alive (handle RuntimeError below)
            return True

    def load(
        self,
        action_data: Dict[str, Tuple[List[float], List[float], List[bool], object, Optional[float]]],
        # action → (times_rel, signals, flags, bfit|None, t_ref_rel|None)
    ) -> None:
        # Hide all existing panels (skip any whose C++ object was deleted)
        stale_keys: list[str] = []
        for key, p in list(self._panels.items()):
            if self._panel_is_alive(p):
                try:
                    p.setVisible(False)
                except RuntimeError:
                    stale_keys.append(key)
            else:
                stale_keys.append(key)
        for k in stale_keys:
            self._panels.pop(k, None)

        colors = {}
        for i, act in enumerate(action_data):
            colors[act] = _ACTION_COLORS[i % len(_ACTION_COLORS)]

        for col_idx, (action, (times, sigs, flags, bfit, t_ref)) in enumerate(action_data.items()):
            row = col_idx // 2
            col = col_idx % 2
            panel = self._panels.setdefault(action, ActionSignalPanel())
            panel.setVisible(True)
            self._grid.addWidget(panel, row, col)
            panel.load(action, times, sigs, flags, bfit, colors[action], t_ref)

        # Remove panels for actions no longer present
        stale = [k for k in self._panels if k not in action_data]
        for k in stale:
            w = self._panels.pop(k)
            self._grid.removeWidget(w)
            w.deleteLater()


# ---------------------------------------------------------------------------
# Top-level widget: device sub-tabs
# ---------------------------------------------------------------------------

class InletSignalsWidget(QWidget):
    """
    Scrollable per-action signal panels for all devices of one inlet.
    Call load(prep, ir) whenever the selected inlet changes.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        lay.addWidget(self._tabs)

        self._device_pages: Dict[str, _DevicePage] = {}

    def load(self, prep, ir) -> None:
        """
        prep: InletPrep  (has ms_by_device, lv_time_start)
        ir:   InletProcessingResult  (has block_fits)
        """
        t0 = prep.lv_time_start

        for dev in _DEVICES:
            ms = prep.ms_by_device.get(dev)
            if ms is None or ms.resolved_path is None:
                # Hide tab if this device has no data
                idx = self._tab_index(dev)
                if idx >= 0:
                    self._tabs.setTabVisible(idx, False)
                continue

            dev_fits = ir.block_fits.get(dev, {})

            # Group signals by action
            action_data: Dict[str, Tuple] = {}
            for row in ms.signals:
                act = row.action
                if act not in action_data:
                    action_data[act] = ([], [], [], dev_fits.get(act), None)
                times_rel, sigs, flags, bfit, _ = action_data[act]
                times_rel.append(row.lv_time - t0)
                sigs.append(row.signal)

            # Attach outlier flags and t_ref per action
            resolved = {}
            for act, (times_rel, sigs, _, bfit, _) in action_data.items():
                flags = []
                t_ref = None
                if bfit is not None:
                    flags = list(bfit.outlier_flags)
                    t_ref = bfit.t_ref - t0
                resolved[act] = (times_rel, sigs, flags, bfit, t_ref)

            page = self._get_or_create_page(dev)
            page.load(resolved)
            idx = self._tab_index(dev)
            if idx >= 0:
                self._tabs.setTabVisible(idx, True)

    # ------------------------------------------------------------ internals

    def _get_or_create_page(self, dev: str) -> _DevicePage:
        if dev not in self._device_pages:
            page = _DevicePage()
            self._device_pages[dev] = page
            self._tabs.addTab(page, dev)
        return self._device_pages[dev]

    def _tab_index(self, dev: str) -> int:
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == dev:
                return i
        return -1
