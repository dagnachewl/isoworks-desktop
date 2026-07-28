"""
ngam_signal_fit_widget.py
=========================
Reusable PyQt5 widget that displays Isotopx SMS signal fits and lets the
user switch between fit models in real time.

Usage
-----
    result = fit_inlet(sms_data, "4He", selected_model="Linear")
    panel = SMSFitPanel()
    panel.load(result)

The widget emits ``model_selected(str)`` when the user changes the model.
All five fits are pre-computed; switching models only re-draws the curve.
"""
from __future__ import annotations

import math
from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QButtonGroup,
    QRadioButton, QSizePolicy, QFrame, QGridLayout, QGroupBox,
)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from ngam_signal_fitter import MODELS, FitResult, SignalFitResult, select_auto_model
from plot_utils import attach_hover_tooltip

# -- model display properties -----------------------------------------------
_MODEL_COLORS = {
    "Auto":        "#4527A0",   # deep purple (meta-model)
    "Average":     "#9E9E9E",   # grey
    "Linear":      "#1565C0",   # dark blue
    "Poly2":       "#E65100",   # deep orange
    "Poly3":       "#2E7D32",   # dark green
    "Exponential": "#AD1457",   # dark pink
}

_MODEL_LABELS = {
    "Auto":        "Auto (AICc)",
    "Average":     "Average",
    "Linear":      "Linear",
    "Poly2":       "Poly (2nd)",
    "Poly3":       "Poly (3rd)",
    "Exponential": "Exponential",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_val(v: float, sig: int = 4) -> str:
    """Format a float in scientific notation, or '—' if NaN."""
    if v is None or math.isnan(v):
        return "—"
    return f"{v:.{sig}e}"


def _fmt_r2(v: float) -> str:
    if v is None or math.isnan(v):
        return "—"
    return f"{v:.5f}"


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class SMSFitPanel(QWidget):
    """
    Panel showing:
      • matplotlib chart – net signal scatter + fitted curve overlay
      • horizontal radio buttons – one per model
      • metrics strip – value at t=0, uncertainty, R², n points, status
    """

    model_selected = pyqtSignal(str)   # emitted when user changes model

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._result: Optional[SignalFitResult] = None
        self._init_ui()

    # ------------------------------------------------------------------ UI --

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # -- title label -------------------------------------------------------
        self._lbl_title = QLabel()
        self._lbl_title.setAlignment(Qt.AlignCenter)
        self._lbl_title.setStyleSheet("font-weight: bold; font-size: 11px;")
        root.addWidget(self._lbl_title)

        # -- matplotlib canvas -------------------------------------------------
        self._fig = Figure(figsize=(5, 3), constrained_layout=True)
        self._ax  = self._fig.add_subplot(111)
        self._canvas = FigureCanvas(self._fig)
        self._canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self._canvas, stretch=1)

        # -- model selector ----------------------------------------------------
        grp = QGroupBox("Fit model")
        grp.setStyleSheet(
            "QGroupBox { font-size: 10px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        btn_row = QHBoxLayout(grp)
        btn_row.setContentsMargins(4, 4, 4, 4)
        btn_row.setSpacing(12)

        self._btn_group = QButtonGroup(self)
        self._radio: dict[str, QRadioButton] = {}

        for model in MODELS:
            rb = QRadioButton(_MODEL_LABELS[model])
            rb.setStyleSheet(
                f"QRadioButton::indicator {{ width: 12px; height: 12px; }}"
                f"color: {_MODEL_COLORS[model]}; font-weight: bold;"
            )
            self._btn_group.addButton(rb)
            self._radio[model] = rb
            btn_row.addWidget(rb)

        btn_row.addStretch()
        self._btn_group.buttonClicked.connect(self._on_radio_clicked)
        root.addWidget(grp)

        # -- metrics strip -----------------------------------------------------
        metrics_frame = QFrame()
        metrics_frame.setFrameShape(QFrame.StyledPanel)
        metrics_grid = QGridLayout(metrics_frame)
        metrics_grid.setContentsMargins(6, 4, 6, 4)
        metrics_grid.setSpacing(4)

        def _lbl(text: str, bold: bool = False, align=Qt.AlignLeft) -> QLabel:
            lb = QLabel(text)
            lb.setAlignment(align)
            if bold:
                lb.setStyleSheet("font-weight: bold;")
            return lb

        # row 0: labels
        metrics_grid.addWidget(_lbl("Value at t = 0", bold=True), 0, 0)
        metrics_grid.addWidget(_lbl("± Uncertainty",  bold=True), 0, 1)
        metrics_grid.addWidget(_lbl("R²",             bold=True), 0, 2)
        metrics_grid.addWidget(_lbl("Cycles (n)",     bold=True), 0, 3)

        # row 1: values
        self._lbl_val  = _lbl("—", align=Qt.AlignLeft)
        self._lbl_unc  = _lbl("—", align=Qt.AlignLeft)
        self._lbl_r2   = _lbl("—", align=Qt.AlignLeft)
        self._lbl_n    = _lbl("—", align=Qt.AlignLeft)
        self._lbl_val.setStyleSheet("font-size: 12px; font-weight: bold; color: #1A237E;")
        metrics_grid.addWidget(self._lbl_val, 1, 0)
        metrics_grid.addWidget(self._lbl_unc, 1, 1)
        metrics_grid.addWidget(self._lbl_r2,  1, 2)
        metrics_grid.addWidget(self._lbl_n,   1, 3)

        # row 2: status / error message
        self._lbl_status = _lbl("")
        self._lbl_status.setStyleSheet("color: #B71C1C; font-size: 10px;")
        metrics_grid.addWidget(self._lbl_status, 2, 0, 1, 4)

        root.addWidget(metrics_frame)

    # ---------------------------------------------------------------- data --

    def load(
        self,
        result: SignalFitResult,
        isotope_label: Optional[str] = None,
    ) -> None:
        """
        Load a SignalFitResult and refresh the full display.

        Parameters
        ----------
        result : SignalFitResult
            Pre-computed by :func:`ngam_signal_fitter.fit_inlet`.
        isotope_label : str, optional
            Override for the chart title (defaults to isotope + description).
        """
        self._result = result

        if isotope_label is None:
            isotope_label = (
                f"{result.isotope} signal  —  "
                f"Inlet {result.inlet_num}: {result.description}"
            )
        self._lbl_title.setText(isotope_label)

        # Enable/disable radio buttons based on whether the fit succeeded
        # "Auto" is always enabled when at least one base model succeeded
        any_success = any(result.fits[m].success for m in result.fits)
        for model, rb in self._radio.items():
            if model == "Auto":
                rb.setEnabled(any_success)
                rb.setToolTip(
                    f"AICc selects: {select_auto_model(result)}" if any_success else ""
                )
            else:
                rb.setEnabled(result.fits[model].success)
                rb.setToolTip(
                    result.fits[model].message if not result.fits[model].success else
                    f"AICc = {result.fits[model].aicc:.2f}"
                )

        # Set active radio button
        model = result.selected_model
        if model != "Auto" and not result.fits[model].success:
            # Fall back to first successful model
            for m in MODELS:
                if m == "Auto" or result.fits[m].success:
                    model = m
                    break
        self._radio[model].setChecked(True)

        self._refresh_plot(model)
        self._refresh_metrics(model)

    def set_model(self, model: str) -> None:
        """Programmatically switch the active model."""
        if self._result is None or model not in MODELS:
            return
        if model in self._radio:
            self._radio[model].setChecked(True)
        self._refresh_plot(model)
        self._refresh_metrics(model)

    # ------------------------------------------------------------ internals --

    def _on_radio_clicked(self, btn: QRadioButton) -> None:
        label = btn.text()
        model = next((m for m, l in _MODEL_LABELS.items() if l == label), None)
        if model is None or self._result is None:
            return
        self._refresh_plot(model)
        self._refresh_metrics(model)
        self.model_selected.emit(model)

    def _refresh_metrics(self, model: str) -> None:
        if self._result is None:
            return
        if model == "Auto":
            chosen = select_auto_model(self._result)
            fit = self._result.fits[chosen]
            status = f"Auto selected: {chosen}  (AICc = {fit.aicc:.2f})"
        else:
            fit = self._result.fits[model]
            chosen = model
            aicc_str = f"{fit.aicc:.2f}" if not math.isnan(fit.aicc) else "—"
            status = f"AICc = {aicc_str}" if fit.success else fit.message
        self._lbl_val.setText(_fmt_val(fit.value_at_t0))
        self._lbl_unc.setText(_fmt_val(fit.uncertainty))
        self._lbl_r2.setText(_fmt_r2(fit.r_squared))
        n_out = self._result.n_outliers
        n_txt = str(fit.n_points)
        if n_out > 0:
            n_txt += f"  ({n_out} removed)"
        self._lbl_n.setText(n_txt)
        self._lbl_status.setText(status)

    def _refresh_plot(self, model: str) -> None:
        if self._result is None:
            return
        res = self._result
        ax = self._ax
        ax.clear()

        # Resolve Auto → actual chosen model for plotting
        if model == "Auto":
            plot_model = select_auto_model(res)
            label_suffix = f" (Auto→{plot_model})"
        else:
            plot_model = model
            label_suffix = ""

        color = _MODEL_COLORS[model]
        plot_color = _MODEL_COLORS[plot_model]

        # -- split good vs outlier cycles ------------------------------------
        flags = res.outlier_flags if res.outlier_flags else [False] * len(res.t_rel)
        good_t = [t for t, f in zip(res.t_rel, flags) if not f]
        good_s = [s for s, f in zip(res.net_signal, flags) if not f]
        bad_t  = [t for t, f in zip(res.t_rel, flags) if f]
        bad_s  = [s for s, f in zip(res.net_signal, flags) if f]

        # -- net signal data points ------------------------------------------
        sc_good = ax.scatter(
            good_t, good_s,
            s=18, color="#1976D2", zorder=3,
            label=f"{res.isotope} net signal",
        )
        if bad_t:
            sc_bad = ax.scatter(
                bad_t, bad_s,
                marker="x", s=80, color="#D32F2F", linewidths=2, zorder=5,
                label=f"Outlier ({len(bad_t)})",
            )
        else:
            sc_bad = None

        # -- interpolated background (faint) ---------------------------------
        ln_bg = ax.plot(
            res.t_rel, res.bg_interp,
            color="#BDBDBD", linewidth=0.8, linestyle="--", zorder=1,
            label="Background",
        )[0]

        # -- fitted curve ----------------------------------------------------
        fit: FitResult = res.fits[plot_model]
        if fit.success:
            t_curve, y_curve = res.curve_points(plot_model)
            ax.plot(
                t_curve, y_curve,
                color=plot_color, linewidth=1.8, zorder=4,
                label=f"{_MODEL_LABELS[plot_model]} fit{label_suffix}",
            )
            # Mark value at t=0
            ax.axvline(0, color=plot_color, linewidth=0.8, linestyle=":", zorder=2)
            ax.scatter(
                [0], [fit.value_at_t0],
                marker="D", s=40, color=plot_color, zorder=5,
                label=f"t=0: {_fmt_val(fit.value_at_t0)}",
            )

        # -- dynamic y range anchored to the net-signal data, not the background
        # Background is near zero for QMS — including it would pin the axis
        # floor at ~0 and squash the narrow data band into the top of the plot.
        all_y = list(good_s)
        if fit.success:
            _, y_curve = res.curve_points(plot_model)
            all_y.extend(y_curve)
            all_y.append(fit.value_at_t0)
        if all_y:
            y_lo, y_hi = min(all_y), max(all_y)
            span = y_hi - y_lo if y_hi != y_lo else abs(y_hi) * 0.1 or 1e-12
            ax.set_ylim(y_lo - 0.15 * span, y_hi + 0.15 * span)

        # -- axes formatting --------------------------------------------------
        ax.set_xlabel("Time from start (s)", fontsize=8)
        ax.set_ylabel(f"{res.isotope} signal", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.legend(fontsize=7, loc="upper left" if good_s and good_s[-1] < good_s[0] else "lower right")
        n_out = res.n_outliers
        aicc_str = f"AICc={fit.aicc:.1f}" if not math.isnan(fit.aicc) else ""
        title = f"R² = {_fmt_r2(fit.r_squared)}  |  n = {fit.n_points} cycles"
        if aicc_str:
            title += f"  |  {aicc_str}"
        if n_out:
            title += f"  |  {n_out} outlier(s) removed"
        ax.set_title(title, fontsize=8, pad=3)

        # -- hover tooltips ---------------------------------------------------
        _iso = res.isotope
        entries = [
            (sc_good, lambda i, x, y, iso=_iso: f"t = {x:.1f} s\n{iso} = {y:.4e} A"),
            (ln_bg,   lambda i, x, y: f"t = {x:.1f} s\nBG = {y:.4e} A"),
        ]
        if sc_bad is not None:
            entries.append(
                (sc_bad, lambda i, x, y, iso=_iso:
                 f"OUTLIER\nt = {x:.1f} s\n{iso} = {y:.4e} A")
            )
        attach_hover_tooltip(self._canvas, self._ax, entries)

        self._canvas.draw()
