"""
ngam_supplemental_dialog.py
============================
Dialog for adding blanks/standards from other .protocol runs to supplement
the current sequence's calibration inlets.

Each added file can be positioned relative to the primary run:
  • "Before this run"  — inlets shifted to end gap_min before primary start
  • "After this run"   — inlets shifted to begin gap_min after primary end
  • "Actual timestamps"— no shift (use when runs share a continuous timeline)

Usage
-----
    dlg = SupplementalRunsDialog(parent, primary_seq, existing)
    if dlg.exec_() == QDialog.Accepted:
        selected = dlg.get_selected_inlets()  # List[InletPrep]
"""
from __future__ import annotations

import dataclasses
import logging
import math
import os
from typing import Dict, List, Optional, Tuple

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter,
    QListWidget, QListWidgetItem, QPushButton, QLabel,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QFileDialog, QDialogButtonBox,
    QWidget, QMessageBox, QApplication, QComboBox, QDoubleSpinBox,
    QFrame,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor

from ngam_protocol_parser import InletPrep, ProtocolSequence, parse_protocol

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------
_HDR_SS = (
    "QHeaderView::section { background:#3D6A9E; color:white; font-weight:bold;"
    "  padding:3px 6px; border:none; border-right:1px solid #2A4F7C; }"
    "QHeaderView::section:last-section { border-right:none; }"
)
_TABLE_SS = (
    "QTableWidget { border:1px solid #C5CDD9; background:white;"
    "  gridline-color:transparent; alternate-background-color:#F0F4F8; }"
    "QTableWidget::item { padding:3px 5px; }"
    "QTableWidget::item:selected { background:#BBDEFB; color:#000; }"
)
_BTN_ADD = (
    "QPushButton { background:#1565C0; color:white; font-weight:bold;"
    "  border:none; padding:4px 12px; border-radius:4px; }"
    "QPushButton:hover { background:#0D47A1; }"
)
_BTN_REMOVE = (
    "QPushButton { background:#BF360C; color:white; font-weight:bold;"
    "  border:none; padding:4px 12px; border-radius:4px; }"
    "QPushButton:hover { background:#8D2000; }"
    "QPushButton:disabled { background:#B0BEC5; color:#78909C; }"
)

_TYPE_BG = {
    "blank":    QColor("#E3F2FD"),
    "standard": QColor("#F3E5F5"),
}
_INCLUDE_COL = 4

# Positioning options stored per file
_POS_ACTUAL = "actual"
_POS_BEFORE = "before"
_POS_AFTER  = "after"


class SupplementalRunsDialog(QDialog):
    """
    Lets the user pick blanks/standards from other .protocol files to merge
    into the current sequence for calibration purposes.

    Parameters
    ----------
    parent : QWidget
    primary_seq : ProtocolSequence, optional
        The main run being processed.  Used to compute time offsets when
        positioning supplemental runs before/after it.
    existing : list of InletPrep, optional
        Previously selected supplemental inlets (to restore a prior selection).
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        primary_seq: Optional[ProtocolSequence] = None,
        existing: Optional[List[InletPrep]] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Supplemental Calibration Runs")
        self.resize(920, 560)

        self._primary_seq = primary_seq

        # {file_path: {"seq": ProtocolSequence,
        #              "checked": {seq_num: bool},
        #              "position": str,      # _POS_*
        #              "gap_min": float}}
        self._runs: Dict[str, dict] = {}

        self._setup_ui()

        if existing:
            self._restore_existing(existing)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def get_selected_inlets(self) -> List[InletPrep]:
        """
        Return all checked inlets across all loaded files, with time offsets
        applied according to each file's positioning settings.
        """
        selected: List[InletPrep] = []
        for path, info in self._runs.items():
            seq: ProtocolSequence = info["seq"]
            checked: Dict[int, bool] = info["checked"]
            position: str = info["position"]
            gap_sec: float = info["gap_min"] * 60.0

            shift = self._compute_shift(seq, position, gap_sec)

            for prep in seq.inlets:
                if _inlet_type(prep) == "sample":
                    continue
                if not checked.get(prep.seq_num, True):
                    continue

                new_t_start = prep.lv_time_start + shift
                new_t_end   = (prep.lv_time_end + shift
                               if prep.lv_time_end is not None else None)
                inlet = dataclasses.replace(
                    prep,
                    lv_time_start=new_t_start,
                    lv_time_end=new_t_end,
                    is_supplemental=True,
                    source_protocol=path,
                    source_seq_num=prep.seq_num,
                )
                selected.append(inlet)
        return selected

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        # ── Left: file list + positioning controls ────────────────────
        left_w = QWidget()
        left_lay = QVBoxLayout(left_w)
        left_lay.setContentsMargins(0, 0, 4, 0)
        left_lay.setSpacing(4)
        left_lay.addWidget(QLabel("<b>Supplemental .protocol files</b>"))

        self._file_list = QListWidget()
        self._file_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._file_list.currentItemChanged.connect(self._on_file_selected)
        left_lay.addWidget(self._file_list, 1)

        # ── Positioning group (shown for selected file) ────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        left_lay.addWidget(sep)

        self._pos_group = QWidget()
        pos_lay = QVBoxLayout(self._pos_group)
        pos_lay.setContentsMargins(0, 2, 0, 4)
        pos_lay.setSpacing(4)

        lbl_pos = QLabel("<b>Time positioning</b>")
        lbl_pos.setStyleSheet("color:#37474F;")
        pos_lay.addWidget(lbl_pos)

        self._combo_pos = QComboBox()
        self._combo_pos.addItem("Use actual timestamps", _POS_ACTUAL)
        self._combo_pos.addItem("Before primary run", _POS_BEFORE)
        self._combo_pos.addItem("After primary run",  _POS_AFTER)
        self._combo_pos.currentIndexChanged.connect(self._on_pos_changed)
        pos_lay.addWidget(self._combo_pos)

        gap_row = QHBoxLayout()
        gap_row.addWidget(QLabel("Gap from run:"))
        self._spin_gap = QDoubleSpinBox()
        self._spin_gap.setRange(0.0, 10080.0)   # up to 1 week
        self._spin_gap.setValue(30.0)
        self._spin_gap.setSuffix(" min")
        self._spin_gap.setDecimals(1)
        self._spin_gap.setSingleStep(5.0)
        self._spin_gap.setEnabled(False)
        self._spin_gap.valueChanged.connect(self._on_gap_changed)
        gap_row.addWidget(self._spin_gap)
        pos_lay.addLayout(gap_row)

        self._lbl_shift_preview = QLabel("")
        self._lbl_shift_preview.setStyleSheet("color:#546E7A; font-size:10px;")
        self._lbl_shift_preview.setWordWrap(True)
        pos_lay.addWidget(self._lbl_shift_preview)

        self._pos_group.setEnabled(False)
        left_lay.addWidget(self._pos_group)

        btn_row = QHBoxLayout()
        self._btn_add_file = QPushButton("Add File…")
        self._btn_add_file.setStyleSheet(_BTN_ADD)
        self._btn_add_file.clicked.connect(self._add_file)
        btn_row.addWidget(self._btn_add_file)

        self._btn_remove_file = QPushButton("Remove")
        self._btn_remove_file.setStyleSheet(_BTN_REMOVE)
        self._btn_remove_file.setEnabled(False)
        self._btn_remove_file.clicked.connect(self._remove_file)
        btn_row.addWidget(self._btn_remove_file)
        left_lay.addLayout(btn_row)

        splitter.addWidget(left_w)

        # ── Right: inlet table ────────────────────────────────────────
        right_w = QWidget()
        right_lay = QVBoxLayout(right_w)
        right_lay.setContentsMargins(4, 0, 0, 0)
        right_lay.setSpacing(4)

        self._inlet_label = QLabel("Select a file on the left to see its inlets.")
        right_lay.addWidget(self._inlet_label)

        _headers = ["#", "Label", "Type", "Ref (ccSTP)", "Include"]
        self._inlet_tbl = QTableWidget()
        self._inlet_tbl.setStyleSheet(_TABLE_SS)
        self._inlet_tbl.setAlternatingRowColors(True)
        self._inlet_tbl.setShowGrid(False)
        self._inlet_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._inlet_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._inlet_tbl.setColumnCount(len(_headers))
        self._inlet_tbl.setHorizontalHeaderLabels(_headers)
        self._inlet_tbl.horizontalHeader().setStyleSheet(_HDR_SS)
        self._inlet_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for _c in (0, 2, 3, 4):
            self._inlet_tbl.horizontalHeader().setSectionResizeMode(
                _c, QHeaderView.ResizeToContents
            )
        self._inlet_tbl.verticalHeader().setVisible(False)
        self._inlet_tbl.itemChanged.connect(self._on_include_changed)
        right_lay.addWidget(self._inlet_tbl, 1)

        sel_row = QHBoxLayout()
        btn_all = QPushButton("Select All")
        btn_all.clicked.connect(self._select_all)
        btn_none = QPushButton("Deselect All")
        btn_none.clicked.connect(self._deselect_all)
        sel_row.addWidget(btn_all)
        sel_row.addWidget(btn_none)
        sel_row.addStretch(1)
        right_lay.addLayout(sel_row)

        splitter.addWidget(right_w)
        splitter.setSizes([290, 600])
        root.addWidget(splitter, 1)

        bbox = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bbox.accepted.connect(self.accept)
        bbox.rejected.connect(self.reject)
        root.addWidget(bbox)

    # ------------------------------------------------------------------
    # File management
    # ------------------------------------------------------------------

    def _add_file(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add Supplemental Protocol File", "",
            "Protocol Files (*.protocol);;All Files (*)",
        )
        for path in paths:
            path = os.path.abspath(path)
            if path in self._runs:
                continue
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                seq = parse_protocol(path, load_ms_files=False, read_inlet_state=False)
                seq._ms_loaded = False
            except Exception as exc:
                QApplication.restoreOverrideCursor()
                QMessageBox.warning(self, "Parse Error",
                                    f"Could not parse:\n{path}\n\n{exc}")
                continue
            finally:
                QApplication.restoreOverrideCursor()

            checked: Dict[int, bool] = {
                prep.seq_num: (_inlet_type(prep) != "sample")
                for prep in seq.inlets
            }
            self._runs[path] = {
                "seq": seq,
                "checked": checked,
                "position": _POS_ACTUAL,
                "gap_min": 30.0,
            }
            item = QListWidgetItem(os.path.basename(path))
            item.setData(Qt.UserRole, path)
            item.setToolTip(path)
            self._file_list.addItem(item)

        if self._file_list.count() > 0 and self._file_list.currentRow() < 0:
            self._file_list.setCurrentRow(0)

    def _remove_file(self) -> None:
        item = self._file_list.currentItem()
        if item is None:
            return
        path = item.data(Qt.UserRole)
        self._runs.pop(path, None)
        self._file_list.takeItem(self._file_list.row(item))
        self._inlet_tbl.setRowCount(0)
        self._inlet_label.setText("Select a file on the left to see its inlets.")
        self._pos_group.setEnabled(False)
        self._btn_remove_file.setEnabled(self._file_list.count() > 0)

    # ------------------------------------------------------------------
    # Inlet table
    # ------------------------------------------------------------------

    def _on_file_selected(self, item: Optional[QListWidgetItem]) -> None:
        self._btn_remove_file.setEnabled(item is not None)
        self._pos_group.setEnabled(item is not None)
        if item is None:
            return
        path = item.data(Qt.UserRole)
        info = self._runs.get(path)
        if info is None:
            return

        # Update positioning controls (block signals to avoid feedback)
        self._combo_pos.blockSignals(True)
        self._spin_gap.blockSignals(True)
        pos_idx = self._combo_pos.findData(info["position"])
        self._combo_pos.setCurrentIndex(pos_idx if pos_idx >= 0 else 0)
        self._spin_gap.setValue(info["gap_min"])
        self._spin_gap.setEnabled(info["position"] != _POS_ACTUAL)
        self._combo_pos.blockSignals(False)
        self._spin_gap.blockSignals(False)

        seq: ProtocolSequence = info["seq"]
        n_shown = sum(1 for p in seq.inlets if _inlet_type(p) != "sample")
        self._inlet_label.setText(
            f"{os.path.basename(path)}  —  {len(seq.inlets)} inlet(s),"
            f" {n_shown} non-sample shown"
        )
        self._populate_inlet_table(seq, info["checked"])
        self._update_shift_preview(path)

    def _populate_inlet_table(
        self,
        seq: ProtocolSequence,
        checked: Dict[int, bool],
    ) -> None:
        tbl = self._inlet_tbl
        tbl.blockSignals(True)
        tbl.setRowCount(0)

        non_sample = [p for p in seq.inlets if _inlet_type(p) != "sample"]
        tbl.setRowCount(len(non_sample))

        for row, prep in enumerate(non_sample):
            itype = _inlet_type(prep)
            bg = _TYPE_BG.get(itype, QColor("#FFFFFF"))

            def _cell(txt: str, align=Qt.AlignCenter, _bg=bg) -> QTableWidgetItem:
                it = QTableWidgetItem(txt)
                it.setTextAlignment(align)
                it.setBackground(_bg)
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                return it

            tbl.setItem(row, 0, _cell(str(prep.seq_num)))
            tbl.setItem(row, 1, _cell(prep.inlet_string, Qt.AlignLeft | Qt.AlignVCenter))
            tbl.setItem(row, 2, _cell(itype.capitalize()))
            ref_txt = f"{prep.reference_amount:.4g}" if prep.reference_amount else "—"
            tbl.setItem(row, 3, _cell(ref_txt))

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            is_checked = checked.get(prep.seq_num, True)
            chk.setCheckState(Qt.Checked if is_checked else Qt.Unchecked)
            chk.setData(Qt.UserRole, prep.seq_num)
            chk.setBackground(bg)
            chk.setTextAlignment(Qt.AlignCenter)
            tbl.setItem(row, _INCLUDE_COL, chk)

        tbl.blockSignals(False)

    def _on_include_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != _INCLUDE_COL:
            return
        file_item = self._file_list.currentItem()
        if file_item is None:
            return
        path = file_item.data(Qt.UserRole)
        info = self._runs.get(path)
        if info is None:
            return
        sn = item.data(Qt.UserRole)
        info["checked"][sn] = item.checkState() == Qt.Checked

    def _select_all(self) -> None:
        self._set_all_checked(True)

    def _deselect_all(self) -> None:
        self._set_all_checked(False)

    def _set_all_checked(self, state: bool) -> None:
        tbl = self._inlet_tbl
        tbl.blockSignals(True)
        file_item = self._file_list.currentItem()
        if file_item:
            path = file_item.data(Qt.UserRole)
            info = self._runs.get(path)
            if info:
                for row in range(tbl.rowCount()):
                    chk = tbl.item(row, _INCLUDE_COL)
                    if chk:
                        sn = chk.data(Qt.UserRole)
                        info["checked"][sn] = state
                        chk.setCheckState(Qt.Checked if state else Qt.Unchecked)
        tbl.blockSignals(False)

    # ------------------------------------------------------------------
    # Positioning controls
    # ------------------------------------------------------------------

    def _on_pos_changed(self) -> None:
        file_item = self._file_list.currentItem()
        if file_item is None:
            return
        path = file_item.data(Qt.UserRole)
        info = self._runs.get(path)
        if info is None:
            return
        pos = self._combo_pos.currentData()
        info["position"] = pos
        self._spin_gap.setEnabled(pos != _POS_ACTUAL)
        self._update_shift_preview(path)

    def _on_gap_changed(self) -> None:
        file_item = self._file_list.currentItem()
        if file_item is None:
            return
        path = file_item.data(Qt.UserRole)
        info = self._runs.get(path)
        if info is None:
            return
        info["gap_min"] = self._spin_gap.value()
        self._update_shift_preview(path)

    def _update_shift_preview(self, path: str) -> None:
        info = self._runs.get(path)
        if info is None:
            self._lbl_shift_preview.setText("")
            return
        shift = self._compute_shift(info["seq"], info["position"], info["gap_min"] * 60.0)
        if shift == 0.0:
            self._lbl_shift_preview.setText("No time shift applied.")
        else:
            sign = "+" if shift >= 0 else "−"
            hrs = abs(shift) / 3600.0
            self._lbl_shift_preview.setText(
                f"Shift: {sign}{hrs:.2f} h ({sign}{abs(shift)/60:.1f} min)"
            )

    def _compute_shift(
        self,
        seq: ProtocolSequence,
        position: str,
        gap_sec: float,
    ) -> float:
        """Return the seconds to add to all supplemental inlet timestamps."""
        if position == _POS_ACTUAL or self._primary_seq is None:
            return 0.0

        primary_t_start = self._primary_seq.lv_time_start
        primary_t_end   = self._primary_seq.lv_time_end or primary_t_start

        supp_times = [p.lv_time_start for p in seq.inlets]
        if not supp_times:
            return 0.0

        if position == _POS_BEFORE:
            # Last supplemental inlet ends at primary_start − gap
            supp_t_max = max(
                p.lv_time_end or p.lv_time_start for p in seq.inlets
            )
            return primary_t_start - gap_sec - supp_t_max

        if position == _POS_AFTER:
            # First supplemental inlet starts at primary_end + gap
            supp_t_min = min(supp_times)
            return primary_t_end + gap_sec - supp_t_min

        return 0.0

    # ------------------------------------------------------------------
    # Restore existing selection
    # ------------------------------------------------------------------

    def _restore_existing(self, existing: List[InletPrep]) -> None:
        paths_seen: Dict[str, List[int]] = {}
        for prep in existing:
            src = prep.source_protocol
            if src:
                paths_seen.setdefault(src, []).append(
                    prep.source_seq_num or prep.seq_num
                )

        for path, seq_nums in paths_seen.items():
            if not os.path.isfile(path):
                continue
            if path not in self._runs:
                QApplication.setOverrideCursor(Qt.WaitCursor)
                try:
                    seq = parse_protocol(path, load_ms_files=False, read_inlet_state=False)
                    seq._ms_loaded = False
                except Exception:
                    QApplication.restoreOverrideCursor()
                    continue
                finally:
                    QApplication.restoreOverrideCursor()
                checked = {
                    p.seq_num: (p.seq_num in seq_nums)
                    for p in seq.inlets
                    if _inlet_type(p) != "sample"
                }
                self._runs[path] = {
                    "seq": seq,
                    "checked": checked,
                    "position": _POS_ACTUAL,
                    "gap_min": 30.0,
                }
                item = QListWidgetItem(os.path.basename(path))
                item.setData(Qt.UserRole, path)
                item.setToolTip(path)
                self._file_list.addItem(item)

        if self._file_list.count() > 0:
            self._file_list.setCurrentRow(0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _inlet_type(prep: InletPrep) -> str:
    if prep.is_blank or "blank" in prep.inlet_string.lower():
        return "blank"
    if prep.is_reference and prep.reference_amount > 0:
        return "standard"
    return "sample"
