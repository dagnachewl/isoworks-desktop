"""
trims_electrolysis_create_run_gui.py — TRIMS electrolysis run creation widget for IsoWorks.
Provides TrimsElectrolysisCreateRunWidget for assembling a new electrolysis run by
selecting a workflow, assigning TBA samples to a positioned load list, and saving.
"""
from __future__ import annotations
import sys
import logging
import getpass
from datetime import datetime
from typing import Optional, List, Dict

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QComboBox, QLineEdit, QPushButton,
    QTableView, QCheckBox, QLabel, QFormLayout,
    QMessageBox, QDialog, QAbstractItemView, QHeaderView, QSplitter, QSpinBox,
    QScrollArea, QFrame, QSizePolicy, QGridLayout
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QColor, QBrush, QFont
from PyQt5.QtCore import Qt, pyqtSignal, QTimer

from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from help_browser import make_help_button

# Per-row button styles
_BTN_BASE = (
    "QPushButton { background:#f5f5f5; color:#78909c;"
    " border:1px solid #e0e0e0; border-radius:3px;"
    " padding:0 5px; font-size:11px; font-weight:500; }"
)
_BTN_CLEAR_H = (
    "QPushButton:hover { background:#fff3e0; color:#e65100; border-color:#e65100; }"
    "QPushButton:pressed { background:#ffe0b2; color:#bf360c; border-color:#bf360c; }"
)
_BTN_DELETE_H = (
    "QPushButton:hover { background:#ffebee; color:#c62828; border-color:#ef9a9a; }"
    "QPushButton:pressed { background:#ffcdd2; color:#b71c1c; border-color:#c62828; }"
)

# CELL TYPE CONSTANTS (mirror VBA)
CELL_EXCLUDED  = -1
CELL_LOCKED    = -2
CELL_UNKNOWN   =  0
CELL_SPIKE     =  3
CELL_DEADWATER =  6
NO_LAST_CELL   = -9999
# (type_int) -> (label, bg_hex)
CELL_TYPE_META = {
    CELL_EXCLUDED:  ("Excluded", "#bdbdbd"),
    CELL_LOCKED:    ("Locked",   "#ffe082"),
    CELL_UNKNOWN:   ("Unknown",  "#ffffff"),
    CELL_SPIKE:     ("Spike",    "#ffebee"),
    CELL_DEADWATER: ("Blank",    "#e3f2fd"),
    7:              ("Control",  "#e0f2f1"),
}

# =============================================================================
# LOAD LIST COLUMN LAYOUT
# 0: Cell ID   (narrow, fixed)
# 1: OurLabID  (medium, resize to content)
# 2: AnalysisID(medium, resize to content)
# 3: Type      (narrow, fixed)
#
# CHANGE from original:
#   OLD → Cell ID(0), Type(1), OurLabID(2), AnalysisID(3), Name(4)
#   NEW → Cell ID(0), OurLabID(1), AnalysisID(2), Type(3)
#   Name column dropped (OurLabID already identifies the sample).
#
# AVAILABLE SAMPLES COLUMN LAYOUT
# 0: OurLabID   1: Name   2: SubmissionID   3: Priority
#   CHANGE: col 2 was SubmissionName → now SubmissionID
# =============================================================================

LL_COL_CELL   = 0   # Load List: Cell ID
LL_COL_LAB    = 1   # Load List: OurLabID  (carries SID/Prefix in UserRole)
LL_COL_AID    = 2   # Load List: AnalysisID
LL_COL_TYPE   = 3   # Load List: Type string
LL_COL_NAME   = 4   # Load List: Sample Name (sName, stretched)
LL_COL_BTN    = 5   # Load List: Clear/Delete button (setIndexWidget)



# =============================================================================
# CellTile  — one physical cell position on the visual tray map
# =============================================================================
class CellTile(QFrame):
    """
    A colored tile representing one electrolysis cell slot.

    Shows two text lines (Cell ID / sample label) and a background color
    that matches the load-list row scheme, with one addition: empty
    CELL_UNKNOWN empty slots use white/background so they read as "nothing here yet".
    Filled unknowns use water-blue; all other types follow the load-list colour scheme.

    Clicking a tile selects the corresponding row in tblLoadList.
    """
    clicked = pyqtSignal(int)   # emits model row index

    TILE_W = 68
    TILE_H = 58

    # Border colour: slightly darker than the bg (pre-computed pairs)
    _BORDER = {
        "#ffffff": "#cccccc",  # white (empty unknown)
        "#b3e5fc": "#0288d1",  # water-blue
        "#005f9e": "#003a6e",  # dark blue  (DW)
        "#66bb6a": "#338a3e",  # mid-green  (spike)
        "#ffcc80": "#e65100",  # amber      (locked/ctrl)
        "#a8a8a8": "#787878",  # grey       (excluded)
    }

    def __init__(self, row_idx: int, parent=None):
        super().__init__(parent)
        self.row_idx = row_idx
        self.setFixedSize(self.TILE_W, self.TILE_H)
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 2)
        lay.setSpacing(1)

        self.lbl_id   = QLabel()
        self.lbl_id.setAlignment(Qt.AlignHCenter | Qt.AlignBottom)
        self.lbl_id.setFont(QFont("Segoe UI", 9, QFont.Bold))

        self.lbl_samp = QLabel()
        self.lbl_samp.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.lbl_samp.setFont(QFont("Segoe UI", 8))

        lay.addWidget(self.lbl_id)
        lay.addWidget(self.lbl_samp)

    @staticmethod
    def _tray_bg(ctype: int, is_filled: bool):
        """Return (bg_hex, fg_hex) for tray display."""
        if ctype == CELL_EXCLUDED:            return "#a8a8a8", "#505050"
        if ctype == CELL_DEADWATER:           return "#005f9e", "#ffffff"
        if ctype == CELL_SPIKE:               return "#66bb6a", "#1b5e20"
        if ctype in (CELL_LOCKED, 7):         return "#ffcc80", "#7f3f00"
        # CELL_UNKNOWN: lime-green when empty (available), water-blue when filled
        return ("#b3e5fc", "#01579b") if is_filled else ("#ffffff", "#555555")

    def update_cell(self, cell_id: str, sample: str, ctype: int,
                    is_filled: bool, type_str: str = "", is_selected: bool = False):
        bg, fg = self._tray_bg(ctype, is_filled)
        border = self._BORDER.get(bg, "#909090")

        fm = self.lbl_samp.fontMetrics()
        elided = fm.elidedText(sample, Qt.ElideRight, self.TILE_W - 8)
        self.lbl_id.setText(cell_id)
        self.lbl_samp.setText(elided)

        ring = "2px solid #1565c0" if is_selected else f"1px solid {border}"
        ss = (
            f"QFrame {{ background:{bg}; border:{ring}; border-radius:4px; }}"
            f"QLabel {{ color:{fg}; background:transparent; border:none; }}"
        )
        self.setStyleSheet(ss)
        self.setEnabled(ctype != CELL_EXCLUDED)

        tip_sample = sample if sample else "— empty"
        self.setToolTip(f"Cell {cell_id}  |  {type_str}  |  {tip_sample}")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            self.clicked.emit(self.row_idx)
        super().mousePressEvent(event)


# =============================================================================
# TrayMapWidget  — two-row visual tray (port of VBA RepaintLoadList layout)
# =============================================================================
class TrayMapWidget(QWidget):
    """
    Displays all cells in the current run template as a two-row tile grid,
    mirroring the Access form's subform layout produced by RepaintLoadList.

    Two-row split rule (identical to VBA):
      Row 0 : cells[ 0 .. floor(n/2)-1 ]
      Row 1 : cells[ floor(n/2) .. n-1 ]

    Updates:
      • model.dataChanged       → lightweight _patch_tile()  (single-cell repaint)
      • model.layoutChanged /
        rowsInserted / rowsRemoved → full repaint_tray()
      • tblLoadList selection   → highlight_selection()  (blue ring on tile)

    Clicking a tile selects the matching row in tblLoadList and scrolls to it.
    """

    def __init__(self, model: QStandardItemModel, tbl_view: QTableView, parent=None):
        super().__init__(parent)
        self._model    = model
        self._tbl      = tbl_view
        self._tiles: list = []

        # --- Scroll area wrapping a fixed inner grid ----------------------
        self._inner = QWidget()
        self._grid  = QGridLayout(self._inner)
        self._grid.setSpacing(1)
        self._grid.setContentsMargins(6, 1, 6, 4)

        scroll = QScrollArea()
        scroll.setWidget(self._inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedHeight(CellTile.TILE_H * 2 + self._grid.spacing() + 22)  # 2 rows + gap + margins

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        # --- Model signal connections ------------------------------------
        model.dataChanged.connect(self._on_data_changed)
        model.layoutChanged.connect(self.repaint_tray)
        model.rowsInserted.connect(self.repaint_tray)
        model.rowsRemoved.connect(self.repaint_tray)

    # ------------------------------------------------------------------
    def repaint_tray(self, *_args):
        """Full rebuild — clears all tiles and re-creates from model."""
        # Remove and schedule old tiles for deletion
        for tile in self._tiles:
            self._grid.removeWidget(tile)
            tile.deleteLater()
        self._tiles.clear()

        n = self._model.rowCount()
        if n == 0:
            return

        split = max(n // 2, 1)

        sel = set()
        if self._tbl.selectionModel():
            sel = {idx.row() for idx in self._tbl.selectionModel().selectedRows()}

        for i in range(n):
            tile = self._make_tile(i, sel)
            grid_row = 0 if i < split else 1
            grid_col = i if i < split else (i - split)
            self._grid.addWidget(tile, grid_row, grid_col)
            self._tiles.append(tile)

    def _make_tile(self, i: int, selected_rows: set) -> CellTile:
        cell_id  = self._model.item(i, LL_COL_CELL).text()
        lab_text = self._model.item(i, LL_COL_LAB).text()
        ctype    = self._model.item(i, LL_COL_CELL).data(Qt.UserRole + 2) or CELL_UNKNOWN
        type_str = CELL_TYPE_META.get(ctype, ("?",))[0]

        tile = CellTile(i)
        tile.update_cell(cell_id, lab_text, ctype, bool(lab_text),
                         type_str=type_str, is_selected=(i in selected_rows))
        tile.clicked.connect(self._on_tile_clicked)
        return tile

    def _on_data_changed(self, top_left, bottom_right, _roles):
        """Lightweight: repaint only the tiles whose model rows changed."""
        sel = set()
        if self._tbl.selectionModel():
            sel = {idx.row() for idx in self._tbl.selectionModel().selectedRows()}
        for row in range(top_left.row(), bottom_right.row() + 1):
            if 0 <= row < len(self._tiles):
                cell_id  = self._model.item(row, LL_COL_CELL).text()
                lab_text = self._model.item(row, LL_COL_LAB).text()
                ctype    = self._model.item(row, LL_COL_CELL).data(Qt.UserRole + 2) or CELL_UNKNOWN
                type_str = CELL_TYPE_META.get(ctype, ("?",))[0]
                self._tiles[row].update_cell(
                    cell_id, lab_text, ctype, bool(lab_text),
                    type_str=type_str, is_selected=(row in sel)
                )

    def _on_tile_clicked(self, row_idx: int):
        """Sync list selection and scroll."""
        self._tbl.clearSelection()
        self._tbl.selectRow(row_idx)
        self._tbl.scrollTo(self._model.index(row_idx, 0),
                           QAbstractItemView.PositionAtCenter)
        self.highlight_selection()

    def highlight_selection(self):
        """
        Repaint tile borders to show/clear the selection ring.
        Called when tblLoadList selection changes.
        """
        sel = set()
        if self._tbl.selectionModel():
            sel = {idx.row() for idx in self._tbl.selectionModel().selectedRows()}
        for i, tile in enumerate(self._tiles):
            if i >= self._model.rowCount():
                break
            cell_id  = self._model.item(i, LL_COL_CELL).text()
            lab_text = self._model.item(i, LL_COL_LAB).text()
            ctype    = self._model.item(i, LL_COL_CELL).data(Qt.UserRole + 2) or CELL_UNKNOWN
            type_str = CELL_TYPE_META.get(ctype, ("?",))[0]
            tile.update_cell(cell_id, lab_text, ctype, bool(lab_text),
                             type_str=type_str, is_selected=(i in sel))


# --- Control Sample Dialog ---
class TrimsAddControlSampleDialog(QDialog):
    def __init__(self, load_model, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Control Sample")
        self.resize(500, 350)
        self.load_model = load_model

        self._init_ui()
        self._load_controls()
        self._load_positions()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.cmbSample  = QComboBox()
        self.cmbPosFrom = QComboBox()
        self.cmbPosTo   = QComboBox()
        self.chkAll     = QCheckBox("Fill All Empty Slots")

        form.addRow("Control Sample:", self.cmbSample)
        form.addRow("Position From:",  self.cmbPosFrom)
        form.addRow("Position To:",    self.cmbPosTo)
        form.addRow("",                self.chkAll)
        layout.addLayout(form)

        btns = QHBoxLayout()
        self.btnAdd   = QPushButton("Add");   self.btnAdd.clicked.connect(self.add_control)
        self.btnClose = QPushButton("Close"); self.btnClose.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(self.btnAdd)
        btns.addWidget(self.btnClose)
        layout.addLayout(btns)

        self.chkAll.toggled.connect(self._toggle_pos)

    def _toggle_pos(self, checked):
        self.cmbPosFrom.setEnabled(not checked)
        self.cmbPosTo.setEnabled(not checked)

    def _load_controls(self):
        try:
            with db_manager.get_connection() as conn:
                sql = """
                    SELECT rc.SampleID, rc.Prefix, s.sName, st.strLongDescription
                    FROM Sample s
                    JOIN ReferenceControl rc
                      ON s.SampleID = rc.SampleID AND s.Prefix = rc.Prefix
                    JOIN ReferenceControlData rcd
                      ON rc.ReferenceID = rcd.ReferenceID
                    JOIN tblSampleType st
                      ON st.intSampleType = s.SampleType
                    WHERE rcd.AvailableDateTo IS NULL
                      AND s.SampleType IN (3, 6, 7)
                    ORDER BY s.SampleType, rc.SampleID
                """
                for r in conn.execute(text(sql)):
                    txt = f"{r[1]}-{r[0]} : {r[2]} ({r[3]})"
                    self.cmbSample.addItem(txt, (r[0], r[1], r[2]))
        except Exception as e:
            logging.error(f"Load controls failed: {e}")

    def _load_positions(self):
        self.cmbPosFrom.clear()
        self.cmbPosTo.clear()
        for r in range(self.load_model.rowCount()):
            cell_id = self.load_model.item(r, LL_COL_CELL).text()
            self.cmbPosFrom.addItem(cell_id)
            self.cmbPosTo.addItem(cell_id)

    def add_control(self):
        data = self.cmbSample.currentData()
        if not data: return
        sid, pfx, name = data

        fill_all = self.chkAll.isChecked()
        p_from   = self.cmbPosFrom.currentText()
        p_to     = self.cmbPosTo.currentText()
        updated_count = 0

        for r in range(self.load_model.rowCount()):
            cell_id_str = self.load_model.item(r, LL_COL_CELL).text()
            try:
                cell_val = int(cell_id_str)
                from_val = int(p_from) if p_from else 0
                to_val   = int(p_to)   if p_to   else 999999
            except:
                continue

            should_update = fill_all or (from_val <= cell_val <= to_val)

            if should_update:
                self.load_model.item(r, LL_COL_TYPE).setText("Control")
                # Sync the stored cell-type int so color/button logic sees Control (7)
                self.load_model.item(r, LL_COL_CELL).setData(7, Qt.UserRole + 2)
                self.load_model.item(r, LL_COL_LAB).setText(f"{pfx}-{sid}")
                self.load_model.item(r, LL_COL_LAB).setData(sid, Qt.UserRole)
                self.load_model.item(r, LL_COL_LAB).setData(pfx, Qt.UserRole + 1)
                self.load_model.item(r, LL_COL_NAME).setText(name)

                updated_count += 1

        if updated_count > 0:
            QMessageBox.information(self, "Success", f"Added control to {updated_count} cells.")
            self.accept()
        else:
            QMessageBox.warning(self, "None", "No cells matched selection.")


class TrimsElectrolysisCreateRunWidget(QWidget):
    runCreated    = pyqtSignal()
    requestClose  = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.db_dialect = getattr(db_manager, 'dialect', 'SQL_SERVER')

        self.available_samples_model = QStandardItemModel()
        self.run_loadlist_model      = QStandardItemModel()

        self.next_run_id  = 0
        self.cell_config  = {"count": 0, "first_id": 1}
        self.proc_config  = {"spikes": 0, "blanks": 0,
                             "spike_id": (None, None, ""),   # (SampleID, Prefix, sName)
                             "blank_id": (None, None, "")}   # (SampleID, Prefix, sName)
        self.last_spiked_cell_id = NO_LAST_CELL
        self.last_dw_cell_id     = NO_LAST_CELL

        self._init_ui()
        self._connect_signals()

        try:
            db_manager.get_engine()
            self.load_initial_combos()
            self.fetch_next_run_id()
        except Exception as e:
            logging.error(f"Init DB failed: {e}")

    def _sql_concat(self, *args):
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')):
                parts.append(arg)
            else:
                parts.append(
                    f"CAST({arg} AS NVARCHAR(255))"
                    if self.db_dialect == "SQL_SERVER"
                    else str(arg)
                )
        sep = " + " if self.db_dialect == "SQL_SERVER" else " || "
        return sep.join(parts)

    def _init_ui(self):
        main_layout = QVBoxLayout(self)

        # --- Top Bar ---
        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("<b>Create Electrolysis Run:</b>"))
        self.lblRunID = QLineEdit("Auto")
        self.lblRunID.setReadOnly(True)
        self.lblRunID.setFixedWidth(100)
        self.lblRunID.setStyleSheet("font-weight: bold; background: #ecf0f1;")
        top_bar.addWidget(self.lblRunID)
        top_bar.addStretch()

        self.btnReset  = QPushButton("Reset Template")
        self.btnCreate = QPushButton("Create Run")
        self.btnCreate.setStyleSheet(
            "background-color: #27ae60; color: white; font-weight: bold;"
        )
        self.btnClose = QPushButton("Close")

        top_bar.addWidget(self.btnReset)
        top_bar.addWidget(make_help_button(self, "trims_create_enrichment"))
        top_bar.addWidget(self.btnCreate)
        top_bar.addWidget(self.btnClose)
        main_layout.addLayout(top_bar)

        # --- Main Splitter ---
        splitter = QSplitter(Qt.Horizontal)

        # --- Left Panel ---
        left_widget = QWidget()
        left_layout  = QVBoxLayout(left_widget)

        setup_grp = QGroupBox("Run Setup")
        form      = QFormLayout()

        self.cmbWorkflow  = QComboBox()
        self.cmbSystem    = QComboBox()
        self.cmbProcedure = QComboBox()
        self.cmbStatus    = QComboBox()

        self.txtCells     = QLineEdit(); self.txtCells.setReadOnly(True)
        self.txtStartCell = QLineEdit(); self.txtStartCell.setReadOnly(True)

        form.addRow("Workflow:",   self.cmbWorkflow)
        form.addRow("Procedure:",  self.cmbProcedure)
        form.addRow("System:",     self.cmbSystem)

        h_cells = QHBoxLayout()
        h_cells.addWidget(QLabel("Cell Count:")); h_cells.addWidget(self.txtCells)
        h_cells.addWidget(QLabel("Start ID:"));   h_cells.addWidget(self.txtStartCell)
        form.addRow(h_cells)

        form.addRow("Status Filter:", self.cmbStatus)
        setup_grp.setLayout(form)
        left_layout.addWidget(setup_grp)

        avail_grp = QGroupBox("Available Samples (Ready)")
        v_avail   = QVBoxLayout()
        self.tblAvailable = QTableView()
        self.tblAvailable.setModel(self.available_samples_model)
        self.tblAvailable.setSelectionBehavior(QTableView.SelectRows)
        self.tblAvailable.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tblAvailable.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblAvailable.setAlternatingRowColors(True)
        self.tblAvailable.setStyleSheet(
            "QTableView { gridline-color: #deeeff; border: 1px solid #b3d4f5; }"
            "QTableView::item:selected { background: #e1f5fe; color: #01579b; }"
            "QHeaderView::section { background: #e8f4fd; color: #37474f;"
            " padding: 2px 5px; border: none;"
            " border-right: 1px solid #cce5ff;"
            " border-bottom: 1px solid #90caf9;"
            " font-weight: 600; }"
        )
        self.tblAvailable.verticalHeader().hide()
        self.tblAvailable.verticalHeader().setDefaultSectionSize(22)
        v_avail.addWidget(self.tblAvailable)
        avail_grp.setLayout(v_avail)
        left_layout.addWidget(avail_grp, 1)

        splitter.addWidget(left_widget)

        # --- Middle Buttons ---
        mid_widget = QWidget()
        mid_layout  = QVBoxLayout(mid_widget)
        mid_layout.addStretch()
        self.btnAdd       = QPushButton(">>");  self.btnAdd.setFixedWidth(40)
        self.btnAdd.setToolTip("Add all selected to first empty cells")
        self.btnAddAll    = QPushButton(">>>"); self.btnAddAll.setFixedWidth(40)
        self.btnAddAll.setToolTip("Add ALL available samples")
        self.btnRemove    = QPushButton("<");   self.btnRemove.setFixedWidth(40)
        self.btnRemove.setToolTip("Clear selected cell")
        self.btnRemoveAll = QPushButton("<<");  self.btnRemoveAll.setFixedWidth(40)
        self.btnRemoveAll.setToolTip("Clear all unknown samples")
        mid_layout.addWidget(self.btnAdd)
        mid_layout.addWidget(self.btnAddAll)
        mid_layout.addSpacing(20)
        mid_layout.addWidget(self.btnRemove)
        mid_layout.addWidget(self.btnRemoveAll)
        mid_layout.addStretch()
        splitter.addWidget(mid_widget)

        # --- Right Panel ---
        right_widget = QWidget()
        right_layout  = QVBoxLayout(right_widget)

        load_grp  = QGroupBox("Electrolysis Load List (Cells)")
        v_load    = QVBoxLayout()
        self.lblStats = QLabel("Total: 0 | Unknowns: 0 | Spikes: 0 | Blanks: 0")
        v_load.addWidget(self.lblStats)

        self.tblLoadList = QTableView()
        self.tblLoadList.setModel(self.run_loadlist_model)
        self.tblLoadList.setSelectionBehavior(QTableView.SelectRows)
        self.tblLoadList.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tblLoadList.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblLoadList.setAlternatingRowColors(False)  # manual per-row coloring
        self.tblLoadList.setStyleSheet(
            "QTableView { gridline-color: #deeeff; border: 1px solid #b3d4f5; }"
            "QTableView::item:selected { background: #e1f5fe; color: #01579b; }"
            "QHeaderView::section { background: #e8f4fd; color: #37474f;"
            " padding: 2px 5px; border: none;"
            " border-right: 1px solid #cce5ff;"
            " border-bottom: 1px solid #90caf9;"
            " font-weight: 600; }"
        )
        self.tblLoadList.verticalHeader().hide()   # CellID makes row numbers redundant
        self.tblLoadList.verticalHeader().setDefaultSectionSize(22)

        # Column widths set after model is populated
        hdr = self.tblLoadList.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)

        v_load.addWidget(self.tblLoadList)

        self.btnAddControl = QPushButton("Add Control Sample")
        self.btnAddControl.clicked.connect(self.open_add_control_dialog)
        v_load.addWidget(self.btnAddControl)

        load_grp.setLayout(v_load)

        # --- Tray Map: title + toggle sit outside the frame so the
        #     scroll area has full height even when a horizontal scrollbar appears.
        tray_bar = QHBoxLayout()
        tray_bar.setContentsMargins(2, 2, 2, 0)
        self._tray_toggle = QPushButton("▼")
        self._tray_toggle.setFixedWidth(22)
        self._tray_toggle.setFlat(True)
        self._tray_toggle.setToolTip("Collapse / expand tray")
        tray_bar.addWidget(QLabel("<b>Tray Map</b>"))
        tray_bar.addStretch()
        tray_bar.addWidget(self._tray_toggle)

        self.tray_map = TrayMapWidget(self.run_loadlist_model,
                                      self.tblLoadList)

        tray_frame = QFrame()
        tray_frame.setStyleSheet(
            "QFrame { border: 1px solid #90aec8; border-radius: 3px;"
            " background: #f0f6fb; }"
        )
        tray_frame_layout = QVBoxLayout(tray_frame)
        tray_frame_layout.setContentsMargins(2, 2, 2, 2)
        tray_frame_layout.setSpacing(0)
        tray_frame_layout.addWidget(self.tray_map)

        right_layout.addLayout(tray_bar)
        right_layout.addWidget(tray_frame)
        right_layout.addWidget(load_grp, 1)

        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 4)
        main_layout.addWidget(splitter)

        # --- Model headers ---
        # Available samples: OurLabID | Name | SubmissionID | Priority
        self.available_samples_model.setHorizontalHeaderLabels(
            ["OurLabID", "Name", "AnalysisID", "Submission ID", "Priority"]
        )
        # Load list: Cell ID | OurLabID | AnalysisID | Type | Action
        self.run_loadlist_model.setHorizontalHeaderLabels(
            ["Cell ID", "OurLabID", "AnalysisID", "Type", "Sample Name", ""]
        )

    def _apply_loadlist_column_widths(self):
        """
        Called after the template is (re)[built].
        Cell ID and Type are narrow/fixed; OurLabID and AnalysisID resize to content.
        """
        hdr = self.tblLoadList.horizontalHeader()
        hdr.setSectionResizeMode(LL_COL_CELL, QHeaderView.Fixed);           self.tblLoadList.setColumnWidth(LL_COL_CELL, 60)
        hdr.setSectionResizeMode(LL_COL_LAB,  QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(LL_COL_AID,  QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(LL_COL_TYPE, QHeaderView.Fixed);           self.tblLoadList.setColumnWidth(LL_COL_TYPE, 68)
        hdr.setSectionResizeMode(LL_COL_NAME, QHeaderView.Stretch)          # fills remaining space
        hdr.setSectionResizeMode(LL_COL_BTN,  QHeaderView.Fixed);           self.tblLoadList.setColumnWidth(LL_COL_BTN,  64)

    def _connect_signals(self):
        self.cmbWorkflow.currentIndexChanged.connect(self.on_workflow_changed)
        self.cmbSystem.currentIndexChanged.connect(self.on_system_changed)
        self.cmbProcedure.currentIndexChanged.connect(self.on_procedure_changed)
        self.cmbStatus.currentIndexChanged.connect(self.load_available_samples)

        self.btnReset.clicked.connect(self.init_template)

        self.btnAdd.clicked.connect(self.add_selected_samples)
        self.btnAddAll.clicked.connect(self.add_all_samples)
        self.btnRemove.clicked.connect(self.clear_selected_cells)
        self.btnRemoveAll.clicked.connect(self.clear_all_unknowns)

        self.tblAvailable.doubleClicked.connect(lambda idx: self.add_selected_samples())
        self.tblLoadList.doubleClicked.connect(lambda idx: self.clear_selected_cells())
        self.tblLoadList.selectionModel().selectionChanged.connect(
            lambda: self.tray_map.highlight_selection())

        self.btnCreate.clicked.connect(self.on_create_clicked)
        self.btnClose.clicked.connect(self.requestClose.emit)
        self._tray_toggle.clicked.connect(self._toggle_tray)

    def open_add_control_dialog(self):
        dlg = TrimsAddControlSampleDialog(self.run_loadlist_model, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            # Refresh colors and buttons for every row (dialog may have changed
            # multiple rows and already synced UserRole+2 = 7 for Controls)
            for r in range(self.run_loadlist_model.rowCount()):
                ctype    = self.run_loadlist_model.item(r, LL_COL_CELL).data(Qt.UserRole + 2)
                has_samp = bool(self.run_loadlist_model.item(r, LL_COL_LAB).text())
                self._apply_row_colors(r, ctype, has_samp)
                self._refresh_row_button(r)
            self.load_available_samples()
            self.update_counts()


    def _toggle_tray(self):
        visible = self.tray_map.isVisible()
        self.tray_map.setVisible(not visible)
        self._tray_toggle.setText("▶" if visible else "▼")

    # =========================================================================
    # Row coloring & per-row Clear/Delete buttons
    # =========================================================================

    @staticmethod
    def _row_bg(ctype: int, is_filled: bool):
        """Return (bg_hex, fg_hex) for a load-list row.

        Color scheme:
          CELL_DEADWATER  → dark blue / white text
          CELL_SPIKE      → light green
          CELL_LOCKED / 7 → orange (controls)
          CELL_UNKNOWN, filled → light water-blue
          CELL_UNKNOWN, empty  → white
          CELL_EXCLUDED   → grey (non-interactive)
        """
        if ctype == CELL_EXCLUDED:            return "#bdbdbd", "#616161"
        if ctype == CELL_DEADWATER:           return "#005f9e", "#ffffff"
        if ctype == CELL_SPIKE:               return "#a5d6a7", "#1b5e20"
        if ctype in (CELL_LOCKED, 7):         return "#ffcc80", "#bf360c"
        # CELL_UNKNOWN
        return ("#b3e5fc", "#01579b") if is_filled else ("#ffffff", "#333333")

    def _apply_row_colors(self, r: int, ctype: int, is_filled: bool):
        """Paint background and foreground on data columns 0-3 of row r."""
        bg_hex, fg_hex = self._row_bg(ctype, is_filled)
        bg = QBrush(QColor(bg_hex))
        fg = QBrush(QColor(fg_hex))
        for col in range(LL_COL_BTN):          # cols 0-3 only
            it = self.run_loadlist_model.item(r, col)
            if it:
                it.setBackground(bg)
                it.setForeground(fg)

    def _refresh_row_button(self, r: int):
        """
        Create (or replace) the QPushButton widget for load-list row r.

        Two-stage semantics mirror the Access form:
          • Row has a sample  → "Clear"  (orange)
            Wipes OurLabID + AnalysisID; reverts Spike/Blank/Control → CELL_UNKNOWN
            so the slot becomes available for any reassignment.
          • Row has no sample → "Delete" (red)
            Asks for confirmation, then removes the row from the run template.
            (The cell itself is not touched in the DB; this only affects the
            current session's template.)
          • CELL_EXCLUDED rows get no button (already visually disabled).
        """
        ctype    = self.run_loadlist_model.item(r, LL_COL_CELL).data(Qt.UserRole + 2)
        has_samp = bool(self.run_loadlist_model.item(r, LL_COL_LAB).text())

        if ctype == CELL_EXCLUDED:
            self.tblLoadList.setIndexWidget(
                self.run_loadlist_model.index(r, LL_COL_BTN), None)
            return

        btn = QPushButton()
        btn.setFixedHeight(20)

        ctype_str = CELL_TYPE_META.get(ctype, ("Unknown",))[0]
        cell_label = self.run_loadlist_model.item(r, LL_COL_CELL).text()

        if has_samp:
            btn.setText("Clear")
            btn.setStyleSheet(_BTN_BASE + _BTN_CLEAR_H)
            btn.setToolTip(
                f"Clear {ctype_str} assignment\n"
                "Wipes the sample and reverts this slot to Unknown —\n"
                "it can then be re-assigned or filled via Add Control Sample."
            )
            btn.clicked.connect(lambda _checked, row=r: self._on_row_clear(row))
        else:
            btn.setText("Delete")
            btn.setStyleSheet(_BTN_BASE + _BTN_DELETE_H)
            btn.setToolTip(
                f"Remove cell {cell_label} from this run's template.\n"
                "The cell is NOT changed in the database.\n"
                "Reset Template restores all cells."
            )
            btn.clicked.connect(lambda _checked, row=r: self._on_row_delete(row))

        self.tblLoadList.setIndexWidget(
            self.run_loadlist_model.index(r, LL_COL_BTN), btn)

    def _refresh_all_buttons(self):
        """Recreate every row button (call after row insertions/deletions)."""
        for r in range(self.run_loadlist_model.rowCount()):
            self._refresh_row_button(r)

    def _on_row_clear(self, r: int):
        """
        Clear stage: wipe the assigned sample from the slot and revert its
        cell type to CELL_UNKNOWN so it can be re-used freely.
        Works for Spike, Blank, Control, and filled Unknown rows.
        """
        ctype = self.run_loadlist_model.item(r, LL_COL_CELL).data(Qt.UserRole + 2)

        # Wipe sample identity and analysis ID
        self.run_loadlist_model.item(r, LL_COL_LAB).setText("")
        self.run_loadlist_model.item(r, LL_COL_LAB).setData(None, Qt.UserRole)
        self.run_loadlist_model.item(r, LL_COL_LAB).setData(None, Qt.UserRole + 1)
        self.run_loadlist_model.item(r, LL_COL_AID).setText("")
        self.run_loadlist_model.item(r, LL_COL_NAME).setText("")

        # Revert non-unknown types → CELL_UNKNOWN so slot is freely available
        if ctype != CELL_UNKNOWN:
            self.run_loadlist_model.item(r, LL_COL_CELL).setData(CELL_UNKNOWN, Qt.UserRole + 2)
            self.run_loadlist_model.item(r, LL_COL_TYPE).setText("Unknown")

        self._apply_row_colors(r, CELL_UNKNOWN, False)
        self._refresh_row_button(r)
        self.load_available_samples()
        self.update_counts()

    def _on_row_delete(self, r: int):
        """
        Delete stage: remove the row from the run template entirely.
        Only available when the slot has no sample (after a Clear, or for
        naturally empty cells).  Requires explicit confirmation.
        """
        cell_id = self.run_loadlist_model.item(r, LL_COL_CELL).text()
        ctype   = self.run_loadlist_model.item(r, LL_COL_CELL).data(Qt.UserRole + 2)

        if ctype == CELL_EXCLUDED:
            msg = (f"Remove decommissioned cell {cell_id} from the template?\n"
                   f"It will no longer appear in this run's tray view.")
        else:
            msg = (f"Remove cell {cell_id} from the run template?\n\n"
                   f"The cell itself is NOT affected in the database — "
                   f"it simply will not appear in this run.\n"
                   f"You can restore it by resetting the template.\n\n"
                   f"Proceed?")

        if QMessageBox.question(
            self, "Confirm Delete", msg,
            QMessageBox.Yes | QMessageBox.No
        ) == QMessageBox.No:
            return

        self.run_loadlist_model.removeRow(r)
        # Row indices shifted — recreate all buttons with updated row numbers
        self._refresh_all_buttons()
        self.load_available_samples()
        self.update_counts()

    # =========================================================================
    # Data Loading & Cascading Logic
    # =========================================================================

    def load_initial_combos(self):
        with db_manager.get_connection() as conn:
            # 1. Workflows that have an Electrolysis_Enrichment job
            self.cmbWorkflow.addItem("- Select -", None)
            sql_wf = f"""
                SELECT DISTINCT w.WorkflowID,
                       {db_manager.sql_concat("w.WorkflowID", "' - '", "w.WorkflowName")}
                FROM Workflow w
                JOIN WorkflowJob wj ON w.WorkflowID = wj.WorkflowID
                WHERE wj.JobName LIKE 'Electrolysis_Enrichment%'
                ORDER BY w.WorkflowID
            """
            for r in conn.execute(text(sql_wf)):
                self.cmbWorkflow.addItem(r[1], r[0])

            # 2. Procedures (all active — workflow selection may narrow this later)
            self.cmbProcedure.addItem("- Select Procedure -", None)
            sql_proc = f"""
                SELECT p.ProcedureID,
                       {db_manager.sql_concat('p.ProcedureID', "' - '", 'p.ProcedureName')}
                FROM AnalysisProcedure p
                JOIN EnrichmentProcedure ep ON p.ProcedureID = ep.ProcedureID
                WHERE p.IsObsolete = {db_manager.sql_bool(False)}
                ORDER BY p.ProcedureID
            """
            for r in conn.execute(text(sql_proc)):
                self.cmbProcedure.addItem(r[1], r[0])

            # 3. Systems populated by procedure selection
            self.cmbSystem.addItem("- Select System -", None)

            self.cmbStatus.addItem("3 - Ready for Enrichment", 3)
            self.cmbStatus.addItem("All Statuses", -1)

    # -------------------------------------------------------------------------
    # on_workflow_changed
    # looks up the Electrolysis_Enrichment job's ProcedureID for the
    # selected workflow and auto-populates cmbProcedure before cascading.
    # -------------------------------------------------------------------------
    def on_workflow_changed(self):
        wid = self.cmbWorkflow.currentData()
        if not wid:
            self.load_available_samples()
            return

        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text(f"""
                    SELECT ProcedureID
                    FROM WorkflowJob
                    WHERE WorkflowID  = :wid
                      AND JobName LIKE 'Electrolysis_Enrichment%'
                """), {"wid": wid}).fetchone()

            if row and row[0]:
                proc_id = row[0]
                idx = self.cmbProcedure.findData(proc_id)
                if idx >= 0:
                    # Block signal to avoid double-firing on_procedure_changed
                    self.cmbProcedure.blockSignals(True)
                    self.cmbProcedure.setCurrentIndex(idx)
                    self.cmbProcedure.blockSignals(False)
                    # Manually trigger the full cascade:
                    # procedure → system filter → cell load → template → samples
                    self.on_procedure_changed()
                    return

        except Exception as e:
            logging.error(f"Workflow change error: {e}")

        # Fallback: no matching job found — just refresh available samples
        self.load_available_samples()

    def on_procedure_changed(self):
        """
        Cascading logic:
        1. Read SampleSize + DefaultDeviceID from AnalysisProcedure.
        2. Filter Systems by SampleVolume >= SampleSize.
        3. Select default system.
        4. Read spike/blank config from EnrichmentProcedure.
        5. Trigger on_system_changed → init_template.
        """
        proc_id = self.cmbProcedure.currentData()
        if not proc_id: return

        try:
            with db_manager.get_connection() as conn:
                proc_row = conn.execute(text("""
                    SELECT SampleSize, DefaultDeviceID
                    FROM AnalysisProcedure
                    WHERE ProcedureID = :pid
                """), {"pid": proc_id}).fetchone()
                if not proc_row: return

                sample_size       = float(proc_row[0] or 0)
                default_device_id = proc_row[1]

                # Filter systems by volume
                self.cmbSystem.blockSignals(True)
                self.cmbSystem.clear()
                self.cmbSystem.addItem("- Select System -", None)
                sql_sys = f"""
                    SELECT e.EquipmentID,
                           {db_manager.sql_concat('e.EquipmentID', "' - '", 'e.EquipmentName')}
                    FROM Equipment e
                    JOIN ElectrolysisSystem es ON e.EquipmentID = es.ElysSystemID
                    WHERE e.CategoryID = 2 AND es.SampleVolume >= :size
                    ORDER BY e.EquipmentID
                """
                for r in conn.execute(text(sql_sys), {"size": sample_size}):
                    self.cmbSystem.addItem(r[1], r[0])

                if default_device_id:
                    idx = self.cmbSystem.findData(default_device_id)
                    if idx >= 0: self.cmbSystem.setCurrentIndex(idx)

                self.cmbSystem.blockSignals(False)

                # Spike / blank config
                enrich_row = conn.execute(text(f"""
                    SELECT NumberOfSpike, NumberOfDeadWater, SpikeID, DeadWaterID
                    FROM EnrichmentProcedure
                    WHERE ProcedureID = :pid
                """), {"pid": proc_id}).fetchone()

                if enrich_row:
                    self.proc_config["spikes"]   = enrich_row[0] or 0
                    self.proc_config["blanks"]   = enrich_row[1] or 0
                    # _get_valid_control_id returns (SampleID, Prefix)
                    self.proc_config["spike_id"] = self._get_valid_control_id(conn, enrich_row[2], 3)
                    self.proc_config["blank_id"] = self._get_valid_control_id(conn, enrich_row[3], 6)

            if self.cmbSystem.currentData():
                self.on_system_changed()
            else:
                self.init_template()

        except Exception as e:
            logging.error(f"Procedure change error: {e}")

    def _get_valid_control_id(self, conn, default_id, sample_type):
        """
        Return (SampleID, Prefix) for an active reference control.
        Validates default_id first; falls back to first available of sample_type.
        Prefix is always included in the JOIN so samples sharing a SampleID
        across different prefixes are distinguished correctly.
        Returns (None, None) if nothing active is found.
        """
        if default_id:
            row = conn.execute(text("""
                SELECT rc.SampleID, s.Prefix, s.sName
                FROM ReferenceControlData rcd
                JOIN ReferenceControl rc  ON rcd.ReferenceID = rc.ReferenceID
                JOIN Sample s             ON rc.SampleID = s.SampleID
                                        AND rc.Prefix    = s.Prefix
                WHERE rc.SampleID = :sid AND rcd.AvailableDateTo IS NULL
            """), {"sid": default_id}).fetchone()
            if row:
                return row[0], row[1], row[2]

        sql_fb = """
            SELECT TOP 1 rc.SampleID, s.Prefix, s.sName
            FROM ReferenceControl rc
            JOIN ReferenceControlData rcd ON rc.ReferenceID = rcd.ReferenceID
            JOIN Sample s                 ON rc.SampleID = s.SampleID
                                         AND rc.Prefix   = s.Prefix
            WHERE s.SampleType = :stype AND rcd.AvailableDateTo IS NULL
            ORDER BY rc.SampleID
        """
        if self.db_dialect not in ("SQL_SERVER", "ACCESS"):
            sql_fb = sql_fb.replace("TOP 1 ", "") + " LIMIT 1"

        row = conn.execute(text(sql_fb), {"stype": sample_type}).fetchone()
        return (row[0], row[1], row[2]) if row else (None, None, "")

    def on_system_changed(self):
        sys_id = self.cmbSystem.currentData()
        if not sys_id: return

        try:
            with db_manager.get_connection() as conn:
                # Physical tray size for the display fields
                row = conn.execute(text(
                    "SELECT NumberOfPositions FROM Equipment WHERE EquipmentID=:id"
                ), {"id": sys_id}).fetchone()
                equip_count = row[0] or 0 if row else 0

                # Load ALL cells ordered by CellID – includes obsolete and locked.
                # IsObsolete / LockForUnknowns drive classification, rotation, and
                # tray display (mirroring VBA prepareElelctrolysisRun cell-load pass).
                cell_rows = conn.execute(text("""
                    SELECT CellID,
                           COALESCE(IsObsolete,       FALSE) AS IsObsolete,
                           COALESCE(LockForUnknowns,  FALSE) AS LockForUnknowns
                    FROM ElectrolysisCell
                    WHERE SystemID = :sid
                    ORDER BY CellID
                """), {"sid": sys_id}).fetchall()

                self.cell_config["cells"] = [
                    {"cell_id":          r[0],
                     "is_obsolete":      bool(r[1]),
                     "lock_for_unknowns": bool(r[2])}
                    for r in cell_rows
                ]
                active = [c for c in self.cell_config["cells"] if not c["is_obsolete"]]
                self.cell_config["first_id"] = active[0]["cell_id"] if active else 1
                # Use actual DB count (includes excluded) so physical tray matches
                self.cell_config["count"]    = equip_count or len(self.cell_config["cells"])

                # Last spike / DW cell positions from the most recent run on this
                # system, so rotation continues correctly across runs.
                last_run = conn.execute(text(
                    "SELECT MAX(RunID) FROM TRIMS.ElectrolysisRun WHERE ElysSystemID=:sid"
                ), {"sid": sys_id}).fetchone()
                last_run_id = last_run[0] if last_run and last_run[0] else None

                spike_sid, spike_pfx, _sp_name = self.proc_config.get("spike_id", (None, None, ""))
                dw_sid,    dw_pfx,    _dw_name = self.proc_config.get("blank_id", (None, None, ""))

                self.proc_config["last_spiked_cell"] = NO_LAST_CELL
                self.proc_config["last_dw_cell"]     = NO_LAST_CELL

                if last_run_id and spike_sid and spike_pfx:
                    r = conn.execute(text("""
                        SELECT MAX(e.CellID) FROM TRIMS.Electrolysis e
                        JOIN Analysis a ON e.AnalysisID = a.AnalysisID
                        WHERE e.RunID = :rid
                          AND a.SampleID = :sid AND a.Prefix = :pfx
                    """), {"rid": last_run_id, "sid": spike_sid, "pfx": spike_pfx}).fetchone()
                    if r and r[0]:
                        self.proc_config["last_spiked_cell"] = r[0]

                if last_run_id and dw_sid and dw_pfx:
                    r = conn.execute(text("""
                        SELECT MAX(e.CellID) FROM TRIMS.Electrolysis e
                        JOIN Analysis a ON e.AnalysisID = a.AnalysisID
                        WHERE e.RunID = :rid
                          AND a.SampleID = :sid AND a.Prefix = :pfx
                    """), {"rid": last_run_id, "sid": dw_sid, "pfx": dw_pfx}).fetchone()
                    if r and r[0]:
                        self.proc_config["last_dw_cell"] = r[0]

            self.txtCells.setText(str(self.cell_config["count"]))
            self.txtStartCell.setText(str(self.cell_config["first_id"]))
            self._fetch_last_rotation_ids(sys_id)
            self.init_template()

        except Exception as e:
            logging.error(f"System change error: {e}")

    def fetch_next_run_id(self):
        with db_manager.get_connection() as conn:
            res = conn.execute(text("SELECT MAX(RunID) FROM TRIMS.ElectrolysisRun")).fetchone()
            self.next_run_id = (res[0] or 1000) + 1
            self.lblRunID.setText(f"New: ~{self.next_run_id}")

    # -------------------------------------------------------------------------
    # Available Samples
    # Unknowns are distilled before electrolysis → they already have an AnalysisID
    # (Status=3, Distilled).  We fetch and display that AnalysisID here so the
    # operator can confirm the correct analysis is being assigned.
    # Exclusion is by AnalysisID (not SampleID) because the same sample may appear
    # with multiple Analysis rows (e.g. re-analysis).
    # -------------------------------------------------------------------------
    def load_available_samples(self):
        self.available_samples_model.removeRows(0, self.available_samples_model.rowCount())
        wid = self.cmbWorkflow.currentData()
        if not wid: return

        stat = self.cmbStatus.currentData()

        try:
            with db_manager.get_connection() as conn:
                # r0=SampleID  r1=Prefix  r2=sName  r3=OurLabID
                # r4=PriorityID  r5=SubmissionID  r6=SubmissionName(unused)
                # r7=AnalysisID  ← already exists (distilled step created it)
                sql = f"""
                    SELECT s.SampleID, s.Prefix, s.sName,
                           {db_manager.sql_concat("s.Prefix", "'-'", "s.SampleID")},
                           sub.PriorityID, sub.SubmissionID, sub.SubmissionName,
                           a.AnalysisID
                    FROM Sample s
                    JOIN Submission sub ON s.SubmissionID = sub.SubmissionID
                    JOIN Analysis a    ON s.SampleID = a.SampleID
                                      AND s.Prefix   = a.Prefix
                    WHERE a.WorkflowID = :wid
                """
                params = {"wid": wid}
                if stat != -1:
                    sql += " AND a.Status = :stat"
                    params["stat"] = stat
                sql += " ORDER BY sub.PriorityID DESC, s.SampleID"

                # Collect AnalysisIDs already placed in the load list to avoid duplicates.
                # Using AnalysisID (not SampleID) because the same sample can appear
                # in Analysis more than once.
                loaded_aids = set()
                for row in range(self.run_loadlist_model.rowCount()):
                    aid = self.run_loadlist_model.item(row, LL_COL_AID).text()
                    if aid:
                        try: loaded_aids.add(int(aid))
                        except ValueError: pass

                for r in conn.execute(text(sql), params).fetchall():
                    if r[7] in loaded_aids: continue   # r[7] = AnalysisID

                    item_lab = QStandardItem(r[3])           # OurLabID  (col 0)
                    item_lab.setData(r[0], Qt.UserRole)      # SampleID
                    item_lab.setData(r[1], Qt.UserRole + 1)  # Prefix
                    item_lab.setData(r[4], Qt.UserRole + 2)  # PriorityID
                    item_lab.setData(r[7], Qt.UserRole + 3)  # AnalysisID ← carry for transfer

                    self.available_samples_model.appendRow([
                        item_lab,                            # col 0: OurLabID
                        QStandardItem(r[2]),                 # col 1: Name
                        QStandardItem(str(r[7])),            # col 2: AnalysisID
                        QStandardItem(str(r[5])),            # col 3: SubmissionID
                        QStandardItem(str(r[4])),            # col 4: Priority
                    ])

            self.tblAvailable.resizeColumnsToContents()

        except Exception as e:
            logging.error(f"Load available samples failed: {e}")

    # =========================================================================
    # Template (Load List) Logic
    # =========================================================================

    @staticmethod
    def _find_next_rotation_pos(cell_ids: list, last_used_id: int, block_size: int) -> int:
        """
        Port of VBA FindNextRotationPos.

        Returns the start index in cell_ids for the next rotation block.
          - last_used_id not in list (first run / reset) → 0
          - otherwise → last_pos + 1, wrap to 0 if past end
          - if fewer than block_size slots remain after next_pos → wrap to 0
            so the block always fits contiguously
        """
        count = len(cell_ids)
        if count == 0:
            return 0
        try:
            last_pos = cell_ids.index(last_used_id)
        except ValueError:
            return 0                          # not found → first run

        next_pos = last_pos + 1
        if next_pos >= count:
            next_pos = 0
        if (count - next_pos) < block_size:
            next_pos = 0                      # not enough room → wrap to start
        return next_pos

    def _fetch_last_rotation_ids(self, sys_id: int):
        """
        Query the last completed run for this system to find:
          - the highest CellID that held a spike (SampleType = CELL_SPIKE)
          - the highest CellID that held a dead-water blank (SampleType = CELL_DEADWATER)
        Stores results in self.last_spiked_cell_id / self.last_dw_cell_id.
        """
        self.last_spiked_cell_id = NO_LAST_CELL
        self.last_dw_cell_id     = NO_LAST_CELL
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text(
                    "SELECT MAX(RunID) FROM TRIMS.ElectrolysisRun WHERE ElysSystemID = :sid"
                ), {"sid": sys_id}).fetchone()
                if not row or not row[0]:
                    return
                last_run = row[0]

                for attr, stype in (
                    ("last_spiked_cell_id", CELL_SPIKE),
                    ("last_dw_cell_id",     CELL_DEADWATER),
                ):
                    # SampleType from Sample; Prefix in JOIN ensures uniqueness
                    # across samples that share a SampleID under different prefixes
                    r = conn.execute(text(
                        "SELECT MAX(e.CellID) FROM TRIMS.Electrolysis e "
                        "JOIN Analysis a ON e.AnalysisID = a.AnalysisID "
                        "JOIN Sample s   ON s.SampleID = a.SampleID "
                        "             AND s.Prefix   = a.Prefix "
                        "WHERE e.RunID = :rid AND s.SampleType = :st"
                    ), {"rid": last_run, "st": stype}).fetchone()
                    if r and r[0] is not None:
                        setattr(self, attr, r[0])
        except Exception as e:
            logging.error(f"_fetch_last_rotation_ids failed: {e}")

    def init_template(self):
        """
        Port of VBA prepareElelctrolysisRun.

        1. Query ALL physical cells for the system (ORDER BY CellID).
        2. Classify:
             IsObsolete=1      -> CELL_EXCLUDED (-1) grey, disabled, never saved
             LockForUnknowns=1 -> CELL_LOCKED   (-2) amber, enabled, spike/DW ok
             else              -> CELL_UNKNOWN  ( 0) white, receives unknown samples
        3. active_cells = CELL_LOCKED + CELL_UNKNOWN (both in rotation pool).
        4. Spike rotation  via _find_next_rotation_pos over active_cells.
        5. DW rotation     via _find_next_rotation_pos over non-spike active cells.
        6. ALL cells written to model; CELL_EXCLUDED flagged non-selectable.
        """
        self.run_loadlist_model.removeRows(0, self.run_loadlist_model.rowCount())

        sys_id = self.cmbSystem.currentData()
        if not sys_id:
            return

        num_spikes = self.proc_config["spikes"]
        num_blanks = self.proc_config["blanks"]

        # 1+2. Load and classify cells
        all_cells    = []   # [(cell_id, ctype), ...]
        active_cells = []   # [cell_id, ...]  non-excluded
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT CellID, IsObsolete, LockForUnknowns "
                    "FROM ElectrolysisCell WHERE SystemID = :sid ORDER BY CellID"
                ), {"sid": sys_id}).fetchall()
            for cell_id, is_obs, lock_unk in rows:
                if is_obs:
                    ctype = CELL_EXCLUDED
                elif lock_unk:
                    ctype = CELL_LOCKED
                    active_cells.append(cell_id)
                else:
                    ctype = CELL_UNKNOWN
                    active_cells.append(cell_id)
                all_cells.append((cell_id, ctype))
        except Exception as e:
            logging.error(f"init_template cell query failed: {e}")
            return

        if not active_cells:
            show_message(self, "Error",
                         "No active cells found for the selected system.",
                         QMessageBox.Warning)
            return

        cell_type_map = {cid: ct for cid, ct in all_cells}

        # 4. Spike rotation
        if num_spikes > 0:
            pos = self._find_next_rotation_pos(
                active_cells, self.last_spiked_cell_id, num_spikes)
            for k in range(num_spikes):
                cell_type_map[active_cells[(pos + k) % len(active_cells)]] = CELL_SPIKE

        # 5. DW rotation (only CELL_UNKNOWN candidates)
        if num_blanks > 0:
            dw_cands = [c for c in active_cells if cell_type_map[c] == CELL_UNKNOWN]
            if dw_cands:
                pos = self._find_next_rotation_pos(
                    dw_cands, self.last_dw_cell_id, num_blanks)
                for k in range(num_blanks):
                    cell_type_map[dw_cands[(pos + k) % len(dw_cands)]] = CELL_DEADWATER

        # 6. Build model rows
        for cell_id, _ in all_cells:
            ctype    = cell_type_map[cell_id]
            type_str = CELL_TYPE_META.get(ctype, ("Unknown", "#ffffff"))[0]

            sid, pfx, sname = (None, None, "")
            if ctype == CELL_SPIKE:       sid, pfx, sname = self.proc_config["spike_id"]
            elif ctype == CELL_DEADWATER: sid, pfx, sname = self.proc_config["blank_id"]

            item_cell = QStandardItem(str(cell_id))
            item_cell.setData(cell_id, Qt.UserRole)
            item_cell.setData(ctype,   Qt.UserRole + 2)  # type int for save/logic

            # Display as Prefix-SampleID (e.g. J-350, J-10001)
            display = f"{pfx}-{sid}" if sid and pfx else ""
            item_lab = QStandardItem(display)
            item_lab.setData(sid,  Qt.UserRole)
            item_lab.setData(pfx,  Qt.UserRole + 1)

            item_aid  = QStandardItem("")
            item_type = QStandardItem(type_str)
            item_name = QStandardItem(sname or "")

            item_btn  = QStandardItem("")   # placeholder; real widget via setIndexWidget
            items = [item_cell, item_lab, item_aid, item_type, item_name, item_btn]

            if ctype == CELL_EXCLUDED:
                for it in items:
                    it.setFlags(it.flags() & ~Qt.ItemIsSelectable & ~Qt.ItemIsEnabled)

            self.run_loadlist_model.appendRow(items)
            row_idx = self.run_loadlist_model.rowCount() - 1
            self._apply_row_colors(row_idx, ctype, bool(sid))  # initial color
            self._refresh_row_button(row_idx)

        self._apply_loadlist_column_widths()
        self.load_available_samples()
        self.update_counts()

    def _get_first_empty_unknown(self):
        """Return row index of first empty CELL_UNKNOWN slot, or -1."""
        for r in range(self.run_loadlist_model.rowCount()):
            ctype = self.run_loadlist_model.item(r, LL_COL_CELL).data(Qt.UserRole + 2)
            if ctype != CELL_UNKNOWN:
                continue  # Excluded / Locked / Spike / Blank
            if not self.run_loadlist_model.item(r, LL_COL_LAB).text():
                return r
        return -1

    def add_selected_samples(self):
        """
        Port of VBA cmdCreatetmpEnrichmentRun walk loop.

        Walks load-list rows in CellID order:
          CELL_EXCLUDED -> skip (decommissioned)
          CELL_LOCKED   -> skip (no unknowns; use Add Control Sample)
          CELL_SPIKE / CELL_DEADWATER -> skip (pre-assigned)
          CELL_UNKNOWN, empty -> assign next queued sample
        """
        selected_rows = self.tblAvailable.selectionModel().selectedRows()
        if not selected_rows:
            return

        sample_queue = [
            (idx.sibling(idx.row(), 0).data(Qt.UserRole),      # SampleID
             idx.sibling(idx.row(), 0).data(Qt.UserRole + 1),  # Prefix
             idx.sibling(idx.row(), 0).data(Qt.UserRole + 3),  # AnalysisID
             idx.sibling(idx.row(), 1).data())                 # sName (col 1 in available)
            for idx in selected_rows
            if idx.sibling(idx.row(), 0).data(Qt.UserRole)
        ]

        q_idx = 0
        for r in range(self.run_loadlist_model.rowCount()):
            if q_idx >= len(sample_queue):
                break
            ctype = self.run_loadlist_model.item(r, LL_COL_CELL).data(Qt.UserRole + 2)
            if ctype != CELL_UNKNOWN:
                continue  # Excluded / Locked / Spike / Blank
            if self.run_loadlist_model.item(r, LL_COL_LAB).text():
                continue  # already filled
            sid, pfx, aid, sname = sample_queue[q_idx]
            self.run_loadlist_model.item(r, LL_COL_LAB).setText(f"{pfx}-{sid}")
            self.run_loadlist_model.item(r, LL_COL_LAB).setData(sid, Qt.UserRole)
            self.run_loadlist_model.item(r, LL_COL_LAB).setData(pfx, Qt.UserRole + 1)
            if aid:
                self.run_loadlist_model.item(r, LL_COL_AID).setText(str(aid))
            self.run_loadlist_model.item(r, LL_COL_NAME).setText(sname or "")
            self._apply_row_colors(r, CELL_UNKNOWN, True)   # filled → water-blue
            self._refresh_row_button(r)                     # Unknown+sample → "Clear"
            q_idx += 1

        if q_idx > len(sample_queue):
            show_message(self, "Empty Cells available",
                         f"{q_idx - len(sample_queue)} cells still available: "
                         "you need to fill or remove those cells to Create the RUN.",
                         QMessageBox.Warning)
            
        elif q_idx < len(sample_queue):
            logging.info(f"{len(sample_queue) - q_idx} sample(s) not placed: "
                         "no empty Unknown cells remaining.")

        self.load_available_samples()
        self.update_counts()

    def add_all_samples(self):
        self.tblAvailable.selectAll()
        self.add_selected_samples()

    def clear_selected_cells(self):
        rows = self.tblLoadList.selectionModel().selectedRows()
        for idx in rows:
            r     = idx.row()
            ctype = self.run_loadlist_model.item(r, LL_COL_CELL).data(Qt.UserRole + 2)
            if ctype == CELL_UNKNOWN:  # never clear spike/DW/locked/excluded
                self.run_loadlist_model.item(r, LL_COL_LAB).setText("")
                self.run_loadlist_model.item(r, LL_COL_LAB).setData(None, Qt.UserRole)
                self.run_loadlist_model.item(r, LL_COL_AID).setText("")
                self.run_loadlist_model.item(r, LL_COL_NAME).setText("")
                self._apply_row_colors(r, CELL_UNKNOWN, False)  # cleared → white
                self._refresh_row_button(r)                     # → "Delete"
        self.load_available_samples()
        self.update_counts()

    def clear_all_unknowns(self):
        for r in range(self.run_loadlist_model.rowCount()):
            ctype = self.run_loadlist_model.item(r, LL_COL_CELL).data(Qt.UserRole + 2)
            if ctype == CELL_UNKNOWN:
                self.run_loadlist_model.item(r, LL_COL_LAB).setText("")
                self.run_loadlist_model.item(r, LL_COL_LAB).setData(None, Qt.UserRole)
                self.run_loadlist_model.item(r, LL_COL_AID).setText("")
                self.run_loadlist_model.item(r, LL_COL_NAME).setText("")
                self._apply_row_colors(r, CELL_UNKNOWN, False)
                self._refresh_row_button(r)
        self.load_available_samples()
        self.update_counts()

    def update_counts(self):
        tot = self.run_loadlist_model.rowCount()
        unk = spk = blk = std = excl = locked = 0
        for r in range(tot):
            ctype    = self.run_loadlist_model.item(r, LL_COL_CELL).data(Qt.UserRole + 2)
            has_samp = bool(self.run_loadlist_model.item(r, LL_COL_LAB).text())
            if   ctype == CELL_EXCLUDED:             excl   += 1
            elif ctype == CELL_LOCKED:               locked += 1
            elif ctype == CELL_SPIKE:                spk    += 1
            elif ctype == CELL_DEADWATER:            blk    += 1
            elif ctype == 7:                         std    += 1
            elif ctype == CELL_UNKNOWN and has_samp: unk    += 1
        active = tot - excl
        parts = [f"Active: {active}", f"Unknowns: {unk}",
                 f"Spikes: {spk}", f"Blanks: {blk}"]
        if std:    parts.append(f"Controls: {std}")
        if excl:   parts.append(f"Excluded: {excl}")
        if locked: parts.append(f"Locked: {locked}")
        self.lblStats.setText(" | ".join(parts))
        self.tray_map.repaint_tray()

    # =========================================================================
    # Save
    # =========================================================================

    def on_create_clicked(self):
        if not self.cmbSystem.currentData() or not self.cmbProcedure.currentData():
            show_message(self, "Error", "System and Procedure required.", QMessageBox.Warning)
            return

        unk_count = sum(
            1 for r in range(self.run_loadlist_model.rowCount())
            if (self.run_loadlist_model.item(r, LL_COL_CELL).data(Qt.UserRole + 2) == CELL_UNKNOWN
                and self.run_loadlist_model.item(r, LL_COL_LAB).text())
        )
        if unk_count == 0:
            if QMessageBox.question(
                self, "Warning",
                "No unknown samples assigned. Create run anyway?",
                QMessageBox.Yes | QMessageBox.No
            ) == QMessageBox.No:
                return

        self._save_to_db()

    def _save_to_db(self):
        pid  = self.cmbProcedure.currentData()
        wid  = self.cmbWorkflow.currentData()
        eid  = self.cmbSystem.currentData()
        user = getpass.getuser()
        now  = datetime.now()

        try:
            with db_manager.get_connection() as conn:
                wjid = None
                if wid:
                    res = conn.execute(text(
                        "SELECT WorkflowJobID FROM WorkflowJob "
                        "WHERE WorkflowID=:w AND JobName LIKE 'Electrolysis_Enrichment%'"
                    ), {"w": wid}).fetchone()
                    if res: wjid = res[0]

                # Insert run header — sequence assigns RunID
                rid = conn.execute(text("""
                    INSERT INTO TRIMS.ElectrolysisRun
                        (ProcedureID, WorkflowID, WorkflowJobID,
                         ElysSystemID, RunStartTime, CreateDateStamp, CreateUserStamp, RunStatus)
                    VALUES (:pid, :wid, :wjid, :eid, :now, :now, :user, 4)
                    RETURNING RunID
                """), {"pid": pid, "wid": wid, "wjid": wjid,
                       "eid": eid, "now": now, "user": user}).scalar()

                # Pre-fetch SampleType from Sample for every non-excluded row that
                # has a sample assigned.  A single JOIN query avoids N round-trips.
                # Result: {(sid, pfx): sample_type_int}
                load_pairs = []
                for r in range(self.run_loadlist_model.rowCount()):
                    ct  = self.run_loadlist_model.item(r, LL_COL_CELL).data(Qt.UserRole + 2)
                    sid = self.run_loadlist_model.item(r, LL_COL_LAB).data(Qt.UserRole)
                    pfx = self.run_loadlist_model.item(r, LL_COL_LAB).data(Qt.UserRole + 1)
                    if ct != CELL_EXCLUDED and sid:
                        load_pairs.append((sid, pfx))

                stype_map = {}   # {(sid, pfx): Sample.SampleType}
                if load_pairs:
                    # Build VALUES list for an IN-style query compatible with SQL Server
                    # Use a temp join via STRING_SPLIT or simply fetch individually —
                    # load_pairs is at most ~24 rows so individual fetches are fine.
                    for sid_val, pfx_val in load_pairs:
                        row_st = conn.execute(text(
                            "SELECT SampleType FROM Sample "
                            "WHERE SampleID = :sid AND Prefix = :pfx"
                        ), {"sid": sid_val, "pfx": pfx_val}).fetchone()
                        if row_st:
                            stype_map[(sid_val, pfx_val)] = row_st[0]

                for r in range(self.run_loadlist_model.rowCount()):
                    # CELL_EXCLUDED: visible on tray, never written to Electrolysis
                    ctype = self.run_loadlist_model.item(r, LL_COL_CELL).data(Qt.UserRole + 2)
                    if ctype == CELL_EXCLUDED:
                        continue

                    cell_id = int(self.run_loadlist_model.item(r, LL_COL_CELL).text())
                    sid     = self.run_loadlist_model.item(r, LL_COL_LAB).data(Qt.UserRole)
                    pfx     = self.run_loadlist_model.item(r, LL_COL_LAB).data(Qt.UserRole + 1)

                    if not sid: continue

                    # SampleType for Electrolysis row comes from Sample table
                    db_stype = stype_map.get((sid, pfx), CELL_UNKNOWN)

                    if ctype == CELL_UNKNOWN:
                        # --- Unknown sample -------------------------------------------
                        # AnalysisID already exists from the distillation step (Status=3).
                        # Do NOT INSERT into Analysis.  Just update its status to
                        # STATUS_IN_PROGRESS (4) and use the existing ID.
                        aid_text = self.run_loadlist_model.item(r, LL_COL_AID).text()
                        if not aid_text:
                            logging.warning(f"Row {r}: CELL_UNKNOWN has no AnalysisID — skipping")
                            continue
                        current_aid = int(aid_text)
                        conn.execute(text(
                            "UPDATE Analysis SET Status = 4 "
                            "WHERE AnalysisID = :aid"
                        ), {"aid": current_aid})
                    else:
                        # --- Control sample (Spike / DW / added control) ---------------
                        # These don't come from distillation; create their Analysis row now.
                        # Sequence assigns AnalysisID.
                        current_aid = conn.execute(text("""
                            INSERT INTO Analysis
                                (SampleID, Prefix, Status, Repeats,
                                 WorkflowID, CreateDateStamp, CreateUserStamp)
                            VALUES (:sid, :pfx, 4, 1, :wid, :now, :user)
                            RETURNING AnalysisID
                        """), {"sid": sid, "pfx": pfx,
                               "wid": wid, "now": now, "user": user}).scalar()
                        # Write back so the model shows the newly assigned ID
                        self.run_loadlist_model.item(r, LL_COL_AID).setText(str(current_aid))

                    conn.execute(text("""
                        INSERT INTO TRIMS.Electrolysis
                            (CellID, AnalysisID, RunID,
                             CreateDateStamp, CreateUserStamp, SampleType)
                        VALUES (:cell, :aid, :rid, :now, :user, :stype)
                    """), {"cell": cell_id, "aid": current_aid, "rid": rid,
                           "now": now, "user": user, "stype": db_stype})

                conn.commit()
                self._apply_loadlist_column_widths()   # resize after AID column fills
                show_message(self, "Success", f"Electrolysis Run {rid} created!", QMessageBox.Information)
                self.runCreated.emit()
                self.requestClose.emit()

        except Exception as e:
            logging.error(f"Create Elys Run Error: {e}")
            show_message(self, "Error", f"Failed to create run:\n{e}", QMessageBox.Critical)


class TrimsElectrolysisCreateRunDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create Electrolysis Run")
        self.resize(1200, 750)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.core = TrimsElectrolysisCreateRunWidget(self)
        lay.addWidget(self.core)
        self.core.requestClose.connect(self.reject)
        self.core.runCreated.connect(self.accept)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    dlg = TrimsElectrolysisCreateRunDialog()
    dlg.show()
    sys.exit(app.exec_())