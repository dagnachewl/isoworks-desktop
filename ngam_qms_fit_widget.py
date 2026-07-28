"""
ngam_qms_fit_widget.py
======================
PyQt5 widget that displays fits for all isotopes of one QMS device
(signal fits tab) and isotope ratios (ratio tab) side by side in a
QTabWidget so the plots get the full panel height.
"""
from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout, QScrollArea, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSizePolicy,
)

from ngam_qms_parser import QMSInletData
from ngam_signal_fitter import SignalFitResult
from ngam_signal_fit_widget import SMSFitPanel

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

# KrXe isotope ordering: Kr group then Xe group
_KRXE_ORDER = ["82Kr", "83Kr", "84Kr", "86Kr",
                "129Xe", "131Xe", "132Xe", "134Xe", "136Xe"]


def _fmt_ratio(v: float) -> str:
    if v is None or math.isnan(v):
        return "—"
    return f"{v:.6f}"


class QMSFitWidget(QWidget):
    """
    Two tabs:
      • "Signal Fits"    – scrollable 2-column grid of SMSFitPanel instances
      • "Isotope Ratios" – table of ratio / value / uncertainty

    Usage:
        widget = QMSFitWidget()
        widget.load(qms_data, fits, ratios)
    """

    model_changed = pyqtSignal(str, str)   # (isotope, model_name)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._panels: Dict[str, SMSFitPanel] = {}
        self._init_ui()

    # ------------------------------------------------------------------ UI --

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        root.addWidget(self._tabs)

        # -- Tab 1: Signal Fits (scrollable grid) ----------------------------
        fits_page = QWidget()
        fits_layout = QVBoxLayout(fits_page)
        fits_layout.setContentsMargins(2, 2, 2, 2)
        fits_layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        self._grid_container = QWidget()
        self._grid = QGridLayout(self._grid_container)
        self._grid.setContentsMargins(2, 2, 2, 2)
        self._grid.setSpacing(6)

        scroll.setWidget(self._grid_container)
        fits_layout.addWidget(scroll)

        self._tabs.addTab(fits_page, "Signal Fits")

        # -- Tab 2: Isotope Ratios table -------------------------------------
        ratio_page = QWidget()
        ratio_layout = QVBoxLayout(ratio_page)
        ratio_layout.setContentsMargins(4, 4, 4, 4)
        ratio_layout.setSpacing(4)

        self._ratio_tbl = QTableWidget()
        self._ratio_tbl.setStyleSheet(_TABLE_SS)
        self._ratio_tbl.setAlternatingRowColors(True)
        self._ratio_tbl.setShowGrid(False)
        self._ratio_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._ratio_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._ratio_tbl.setColumnCount(3)
        self._ratio_tbl.setHorizontalHeaderLabels(["Ratio", "Value", "± Uncertainty"])
        self._ratio_tbl.horizontalHeader().setStyleSheet(_HDR_SS)
        self._ratio_tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._ratio_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._ratio_tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._ratio_tbl.verticalHeader().setVisible(False)
        self._ratio_tbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        ratio_layout.addWidget(self._ratio_tbl)

        self._tabs.addTab(ratio_page, "Isotope Ratios")

    # --------------------------------------------------------------- data --

    def load(
        self,
        qms_data: QMSInletData,
        fits: Dict[str, SignalFitResult],
        ratios: Dict[str, Tuple[float, float]],
    ) -> None:
        self._clear_grid()

        isotopes = self._ordered_isotopes(qms_data)

        for i, iso in enumerate(isotopes):
            row = i // 2
            col = i % 2
            panel = self._get_or_create_panel(iso)
            self._grid.addWidget(panel, row, col)
            if iso in fits:
                panel.load(fits[iso], isotope_label=iso)
            else:
                panel.setVisible(False)

        self._populate_ratio_table(ratios)

        # Switch to Signal Fits tab on each new load
        self._tabs.setCurrentIndex(0)

    # ------------------------------------------------------------ internals --

    def _ordered_isotopes(self, qms_data: QMSInletData) -> list:
        if qms_data.device == "QMSKrXe":
            avail = set(qms_data.isotopes)
            return [iso for iso in _KRXE_ORDER if iso in avail]
        return qms_data.isotopes

    def _get_or_create_panel(self, isotope: str) -> SMSFitPanel:
        if isotope not in self._panels:
            panel = SMSFitPanel()
            panel.setMinimumHeight(310)   # ensures scroll bar appears; prevents tight-layout crush
            panel.model_selected.connect(
                lambda model, iso=isotope: self.model_changed.emit(iso, model)
            )
            self._panels[isotope] = panel
        panel = self._panels[isotope]
        panel.setVisible(True)
        return panel

    def _clear_grid(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item and item.widget():
                item.widget().setVisible(False)

    def _populate_ratio_table(
        self, ratios: Dict[str, Tuple[float, float]]
    ) -> None:
        tbl = self._ratio_tbl
        tbl.setRowCount(len(ratios))
        for r, (name, (val, unc)) in enumerate(ratios.items()):
            items = [
                QTableWidgetItem(name),
                QTableWidgetItem(_fmt_ratio(val)),
                QTableWidgetItem(_fmt_ratio(unc)),
            ]
            items[0].setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            items[1].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            items[2].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            for c, it in enumerate(items):
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                tbl.setItem(r, c, it)
        tbl.resizeRowsToContents()
