"""
enrichment_template_editor_gui.py
==================================
Opened from ProcedureManagementWidget → "Edit Template…" when category == 2 (Enrichment).

Layout
------
  Left  │ Template Parameters (cell count, start ID, spike/DW/blank counts + IDs)
        │ Generate button
        │ Legend
  Right │ Tray Map  (same tile style as ElectrolysisCreateRunDialog)
        │ Full load-list table  (colour-coded rows, same palette)
  Bottom│ Save / Cancel

The template is persisted in AnalysisProcedure_Template.
Tile click cycles the cell type; params spinboxes drive auto-generation.
"""
from __future__ import annotations

import logging

from PyQt5.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QLineEdit, QComboBox, QSpinBox, QPushButton,
    QTableView, QScrollArea, QFrame, QHeaderView, QAbstractItemView,
    QDialogButtonBox, QMessageBox, QGridLayout, QSizePolicy
)
from PyQt5.QtCore  import Qt, pyqtSignal
from PyQt5.QtGui   import QStandardItemModel, QStandardItem, QColor, QFont

from db_core   import db_manager
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Cell-type constants  — MUST match trims_electrolysis_create_run_gui.py
# ---------------------------------------------------------------------------
CELL_EXCLUDED  = -1
CELL_LOCKED    =  -2   # Blank / control
CELL_UNKNOWN   =   0   # Unknown sample slot
CELL_SPIKE     =   3
CELL_DEADWATER =   6
CELL_CONTROL   =   7   # additional control variant

# (ctype) → (tray_bg, tray_fg, display_label, short_label)
_CELL_META = {
    CELL_EXCLUDED:  ("#a8a8a8", "#505050", "Excluded",   "Exc"),
    CELL_LOCKED:    ("#ffcc80", "#7f3f00", "Blank/Ctrl", "Blk"),
    CELL_UNKNOWN:   ("#ffffff", "#555555", "Unknown",    ""),
    CELL_SPIKE:     ("#66bb6a", "#1b5e20", "Spike",      "Spk"),
    CELL_DEADWATER: ("#005f9e", "#ffffff", "Dead Water", "DW"),
    CELL_CONTROL:   ("#e0f2f1", "#00695c", "Control",    "Ctl"),
}
_BORDER = {
    "#ffffff": "#cccccc",
    "#b3e5fc": "#0288d1",
    "#005f9e": "#003a6e",
    "#66bb6a": "#338a3e",
    "#ffcc80": "#e65100",
    "#a8a8a8": "#787878",
    "#e0f2f1": "#00796b",
}

# Cycle order when user clicks a tile
_TYPE_CYCLE = [CELL_UNKNOWN, CELL_SPIKE, CELL_DEADWATER, CELL_LOCKED, CELL_CONTROL, CELL_EXCLUDED]

# Table columns
TC_CELL  = 0
TC_TYPE  = 1
TC_LABEL = 2
TC_SAMP  = 3


# ===========================================================================
# TemplateTile  — one clickable colour tile
# ===========================================================================
class TemplateTile(QFrame):
    clicked = pyqtSignal(int)   # emits position index

    W = 56
    H = 38

    def __init__(self, pos_idx: int, parent=None):
        super().__init__(parent)
        self.pos_idx = pos_idx
        self.ctype   = CELL_UNKNOWN
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 1)
        lay.setSpacing(0)

        self.lbl_id   = QLabel()
        self.lbl_id.setAlignment(Qt.AlignHCenter | Qt.AlignBottom)
        self.lbl_id.setFont(QFont("Segoe UI", 7, QFont.Bold))

        self.lbl_type = QLabel()
        self.lbl_type.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.lbl_type.setFont(QFont("Segoe UI", 6))

        lay.addWidget(self.lbl_id)
        lay.addWidget(self.lbl_type)

    def update_tile(self, cell_id: str, ctype: int):
        self.ctype = ctype
        bg, fg, label, short = _CELL_META.get(ctype, ("#ffffff", "#555555", "Unknown", ""))
        border = _BORDER.get(bg, "#909090")
        self.lbl_id.setText(str(cell_id))
        self.lbl_type.setText(short)
        self.setStyleSheet(
            f"QFrame {{ background:{bg}; border:1px solid {border}; border-radius:4px; }}"
            f"QLabel {{ color:{fg}; background:transparent; border:none; }}"
        )
        self.setToolTip(f"Cell {cell_id} — {label}  |  click to cycle type")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.pos_idx)
        super().mousePressEvent(event)


# ===========================================================================
# TrayPreviewWidget  — scrollable two-row tile grid
# ===========================================================================
class TrayPreviewWidget(QWidget):
    tile_clicked = pyqtSignal(int)   # position index

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tiles: list = []

        self._inner = QWidget()
        self._grid  = QGridLayout(self._inner)
        self._grid.setSpacing(2)
        self._grid.setContentsMargins(6, 4, 6, 4)

        scroll = QScrollArea()
        scroll.setWidget(self._inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedHeight(TemplateTile.H * 2 + 4 + 16)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    # ── public API ──────────────────────────────────────────────────────────
    def rebuild(self, cell_ids: list, ctypes: list):
        for t in self._tiles:
            t.setParent(None)
        self._tiles.clear()

        n     = len(cell_ids)
        split = n // 2   # row 0: [0..split-1], row 1: [split..n-1]

        for i, (cid, ctype) in enumerate(zip(cell_ids, ctypes)):
            tile = TemplateTile(i)
            tile.update_tile(cid, ctype)
            tile.clicked.connect(self.tile_clicked)
            self._tiles.append(tile)
            row = 0 if i < split else 1
            col = i if i < split else i - split
            self._grid.addWidget(tile, row, col)

    def update_tile(self, idx: int, cell_id: str, ctype: int):
        if 0 <= idx < len(self._tiles):
            self._tilesidx.update_tile(cell_id, ctype)


# ===========================================================================
# EnrichmentTemplateEditorDialog
# ===========================================================================
class EnrichmentTemplateEditorDialog(QDialog):
    """
    Template editor for enrichment procedures.
    Defines the fixed cell layout (spikes, dead-water, blanks/controls) that
    will be pre-loaded into every new electrolysis run using this procedure.
    """

    def __init__(self, procedure_id, procedure_name: str, parent=None):
        super().__init__(parent)
        self.procedure_id   = procedure_id
        self.procedure_name = procedure_name

        # Parallel lists indexed by cell position (zero-based)
        self._cell_ids:   list = []   # e.g. ["101", "102", …]
        self._cell_types: list = []   # CELL_* int constants
        self._samp_ids:   list = []   # sample IDs (spike/blank); "" for unknowns

        self.setWindowTitle(f"Enrichment Load-List Template — {procedure_name}")
        self.setMinimumSize(1000, 640)
        self.resize(1160, 700)

        self._build_ui()
        self._load_spike_combo()
        self._load_template()   # populate from DB if a template already exists

    # =========================================================================
    # UI construction
    # =========================================================================
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        # ── LEFT: parameters panel ───────────────────────────────────────────
        left = QGroupBox("Template Parameters")
        left.setFixedWidth(270)
        lf = QFormLayout(left)
        lf.setSpacing(7)
        lf.setContentsMargins(10, 14, 10, 10)

        self.spnCellCount = QSpinBox()
        self.spnCellCount.setRange(2, 200)
        self.spnCellCount.setValue(24)

        self.txtStartCell = QLineEdit("101")
        self.txtStartCell.setMaximumWidth(80)

        self.cmbSpikeID = QComboBox()
        self.spnSpikes  = QSpinBox(); self.spnSpikes.setRange(0, 20);  self.spnSpikes.setValue(1)
        self.spnDW      = QSpinBox(); self.spnDW.setRange(0, 20);      self.spnDW.setValue(1)
        self.spnBlanks  = QSpinBox(); self.spnBlanks.setRange(0, 20);  self.spnBlanks.setValue(0)

        self.txtBlankIDs = QLineEdit()
        self.txtBlankIDs.setPlaceholderText("e.g. B-001, B-002")
        self.txtBlankIDs.setToolTip("Comma-separated sample IDs for blanks / controls (optional)")

        lf.addRow("Cell Count:",      self.spnCellCount)
        lf.addRow("Start Cell ID:",   self.txtStartCell)
        lf.addRow("Spike ID:",        self.cmbSpikeID)
        lf.addRow("# Spikes:",        self.spnSpikes)
        lf.addRow("# Dead Water:",    self.spnDW)
        lf.addRow("# Blanks/Ctrl:",   self.spnBlanks)
        lf.addRow("Blank/Ctrl IDs:",  self.txtBlankIDs)

        note = QLabel(
            "<span style='color:#666;font-size:10px;'>"
            "Placement: Spike → Dead Water → Unknown slots → Blanks (end)</span>"
        )
        note.setWordWrap(True)
        lf.addRow(note)

        self.btnGenerate = QPushButton("⚡  Generate Template")
        self.btnGenerate.setStyleSheet(
            "QPushButton { background:#1565c0; color:white; font-weight:600;"
            " border-radius:4px; padding:6px 4px; }"
            "QPushButton:hover   { background:#1976d2; }"
            "QPushButton:pressed { background:#0d47a1; }"
        )
        self.btnGenerate.clicked.connect(self._generate_template)
        lf.addRow(self.btnGenerate)

        # Legend
        legend = QGroupBox("Legend")
        ll = QVBoxLayout(legend)
        ll.setSpacing(3)
        ll.setContentsMargins(6, 8, 6, 6)
        for ctype in [CELL_SPIKE, CELL_DEADWATER, CELL_LOCKED, CELL_CONTROL,
                      CELL_UNKNOWN, CELL_EXCLUDED]:
            bg, fg, label, _ = _CELL_META[ctype]
            swatch = QLabel(f"  {label}  ")
            swatch.setStyleSheet(
                f"background:{bg}; color:{fg}; border:1px solid #bbb;"
                f" border-radius:3px; padding:1px 5px; font-size:10px;"
            )
            swatch.setFixedHeight(20)
            ll.addWidget(swatch)
        ll.addStretch()
        lf.addRow(legend)

        root.addWidget(left)

        # ── RIGHT: tray + table ──────────────────────────────────────────────
        right = QVBoxLayout()
        right.setSpacing(6)

        # Tray title bar
        tray_bar = QHBoxLayout()
        tray_lbl = QLabel("<b>Tray Map</b>")
        hint_lbl = QLabel("  click a tile to cycle its type")
        hint_lbl.setStyleSheet("color:#888; font-size:10px;")
        tray_bar.addWidget(tray_lbl)
        tray_bar.addWidget(hint_lbl)
        tray_bar.addStretch()

        # Tray widget inside thin border frame
        self.tray = TrayPreviewWidget()
        self.tray.tile_clicked.connect(self._on_tile_clicked)

        tray_frame = QFrame()
        tray_frame.setStyleSheet(
            "QFrame { border:1px solid #90aec8; border-radius:3px; background:#f0f6fb; }"
        )
        tfl = QVBoxLayout(tray_frame)
        tfl.setContentsMargins(2, 2, 2, 2)
        tfl.setSpacing(0)
        tfl.addWidget(self.tray)

        right.addLayout(tray_bar)
        right.addWidget(tray_frame)

        # Load-list table
        lbl_table = QLabel("<b>Load-List Template</b>")
        right.addWidget(lbl_table)

        self.table_model = QStandardItemModel(0, 4)
        self.table_model.setHorizontalHeaderLabels(
            ["Cell ID", "Type", "Role", "Sample ID"]
        )

        self.tblTemplate = QTableView()
        self.tblTemplate.setModel(self.table_model)
        self.tblTemplate.setSelectionBehavior(QTableView.SelectRows)
        self.tblTemplate.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblTemplate.setAlternatingRowColors(False)
        self.tblTemplate.verticalHeader().hide()
        self.tblTemplate.verticalHeader().setDefaultSectionSize(22)
        self.tblTemplate.setStyleSheet(
            "QTableView { gridline-color:#deeeff; border:1px solid #b3d4f5; }"
            "QTableView::item:selected { background:#e1f5fe; color:#01579b; }"
            "QHeaderView::section { background:#e8f4fd; color:#37474f;"
            " padding:2px 5px; border:none;"
            " border-right:1px solid #cce5ff;"
            " border-bottom:2px solid #90caf9;"
            " font-weight:600; }"
        )
        hdr = self.tblTemplate.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(TC_LABEL, QHeaderView.Stretch)

        right.addWidget(self.tblTemplate, 1)

        # Dialog Save / Cancel
        btn_box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Save).setStyleSheet(
            "QPushButton { background:#2e7d32; color:white; font-weight:600;"
            " border-radius:4px; padding:4px 14px; }"
            "QPushButton:hover { background:#388e3c; }"
        )
        btn_box.accepted.connect(self._save_template)
        btn_box.rejected.connect(self.reject)
        right.addWidget(btn_box)

        right_widget = QWidget()
        right_widget.setLayout(right)
        root.addWidget(right_widget, 1)

    # =========================================================================
    # Data loading
    # =========================================================================
    def _load_spike_combo(self):
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT R.SampleID, S.sName "
                    "FROM ReferenceControl R "
                    "JOIN Sample S ON R.SampleID = S.SampleID "
                    "WHERE S.SampleType IN (3, 4) "
                    "ORDER BY R.SampleID"
                )).fetchall()
                self.cmbSpikeID.clear()
                self.cmbSpikeID.addItem("", None)
                for r in rows:
                    self.cmbSpikeID.addItem(f"{r[0]} — {r[1]}", r[0])
        except Exception as e:
            logging.warning(f"EnrichmentTemplate spike combo: {e}")

    def _load_template(self):
        """Load an existing template from AnalysisProcedure_Template."""
        if not self.procedure_id:
            return
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT CellID, CellType, SampleID "
                    "FROM AnalysisProcedure_Template "
                    "WHERE ProcedureID = :pid "
                    "ORDER BY Position"
                ), {"pid": self.procedure_id}).fetchall()

            if not rows:
                return

            self._cell_ids   = [str(r[0]) for r in rows]
            self._cell_types = [int(r[1] or 0) for r in rows]
            self._samp_ids   = [r[2] or "" for r in rows]

            # Reflect counts back into the spinboxes
            self.spnCellCount.setValue(len(rows))
            if self._cell_ids:
                self.txtStartCell.setText(self._cell_ids[0])
            self.spnSpikes.setValue(self._cell_types.count(CELL_SPIKE))
            self.spnDW.setValue(self._cell_types.count(CELL_DEADWATER))
            self.spnBlanks.setValue(
                self._cell_types.count(CELL_LOCKED) + self._cell_types.count(CELL_CONTROL)
            )

            # Prefill blank IDs
            blank_ids = [
                sid for sid, ctype in zip(self._samp_ids, self._cell_types)
                if ctype in (CELL_LOCKED, CELL_CONTROL) and sid
            ]
            if blank_ids:
                self.txtBlankIDs.setText(", ".join(str(b) for b in blank_ids))

            # Prefill spike combo
            spike_sid = next(
                (sid for sid, ctype in zip(self._samp_ids, self._cell_types)
                 if ctype == CELL_SPIKE and sid),
                None
            )
            if spike_sid is not None:
                idx = self.cmbSpikeID.findData(spike_sid)
                if idx >= 0:
                    self.cmbSpikeID.setCurrentIndex(idx)

            self._refresh_display()

        except Exception as e:
            logging.warning(f"EnrichmentTemplate load: {e}")

    # =========================================================================
    # Template generation
    # =========================================================================
    def _generate_template(self):
        n      = self.spnCellCount.value()
        n_sp   = self.spnSpikes.value()
        n_dw   = self.spnDW.value()
        n_bl   = self.spnBlanks.value()
        try:    start = int(self.txtStartCell.text())
        except: start = 101

        cell_ids   = [str(start + i) for i in range(n)]
        cell_types = []
        samp_ids   = []

        spike_sid  = self.cmbSpikeID.currentData()
        blank_ids  = [b.strip() for b in self.txtBlankIDs.text().split(",") if b.strip()]
        blank_iter = iter(blank_ids)

        remaining = n

        # Spikes at front
        for _ in range(min(n_sp, remaining)):
            cell_types.append(CELL_SPIKE)
            samp_ids.append(spike_sid or "")
            remaining -= 1

        # Dead water next
        for _ in range(min(n_dw, remaining)):
            cell_types.append(CELL_DEADWATER)
            samp_ids.append("")
            remaining -= 1

        # Unknown slots fill the middle
        blanks_to_place = min(n_bl, remaining)
        for _ in range(remaining - blanks_to_place):
            cell_types.append(CELL_UNKNOWN)
            samp_ids.append("")

        # Blanks at the end
        for _ in range(blanks_to_place):
            cell_types.append(CELL_LOCKED)
            samp_ids.append(next(blank_iter, ""))

        self._cell_ids   = cell_ids
        self._cell_types = cell_types
        self._samp_ids   = samp_ids
        self._refresh_display()

    # =========================================================================
    # Display refresh  (tray + table)
    # =========================================================================
    def _refresh_display(self):
        self.tray.rebuild(self._cell_ids, self._cell_types)
        self._rebuild_table()

    def _rebuild_table(self):
        self.table_model.setRowCount(0)
        for i, (cid, ctype) in enumerate(zip(self._cell_ids, self._cell_types)):
            bg, fg, label, _ = _CELL_META.get(ctype, ("#ffffff", "#555555", "Unknown", ""))
            sid = self._samp_idsi if i < len(self._samp_ids) else ""

            items = [
                QStandardItem(str(cid)),
                QStandardItem(str(ctype)),
                QStandardItem(label),
                QStandardItem(str(sid)),
            ]
            for item in items:
                item.setBackground(QColor(bg))
                item.setForeground(QColor(fg))
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.table_model.appendRow(items)

    # =========================================================================
    # Tile click → cycle type
    # =========================================================================
    def _on_tile_clicked(self, idx: int):
        if not (0 <= idx < len(self._cell_types)):
            return
        current   = self._cell_typesidx
        nxt       = _TYPE_CYCLE[(_TYPE_CYCLE.index(current) + 1) % len(_TYPE_CYCLE)]
        self._cell_typesidx = nxt

        # Assign/clear sample ID automatically
        if nxt == CELL_SPIKE:
            self._samp_idsidx = self.cmbSpikeID.currentData() or ""
        elif nxt not in (CELL_LOCKED, CELL_CONTROL):
            self._samp_idsidx = ""

        self.tray.update_tile(idx, self._cell_idsidx, nxt)

        # Update table row in-place
        bg, fg, label, _ = _CELL_META.get(nxt, ("#ffffff", "#555555", "Unknown", ""))
        sid = self._samp_idsidx
        for col, val in enumerate([self._cell_idsidx, str(nxt), label, str(sid)]):
            item = self.table_model.item(idx, col)
            if item:
                item.setText(val)
                item.setBackground(QColor(bg))
                item.setForeground(QColor(fg))

    # =========================================================================
    # Save
    # =========================================================================
    def _save_template(self):
        if not self._cell_ids:
            QMessageBox.warning(self, "No Template",
                                "Please generate a template first.")
            return

        try:
            with db_manager.get_connection() as conn:
                # Replace existing rows
                conn.execute(
                    text("DELETE FROM AnalysisProcedure_Template WHERE ProcedureID=:pid"),
                    {"pid": self.procedure_id}
                )
                for pos, (cid, ctype) in enumerate(zip(self._cell_ids, self._cell_types)):
                    sid = self._samp_idspos if pos < len(self._samp_ids) else None
                    sid = sid or None   # store NULL not empty string
                    conn.execute(text(
                        "INSERT INTO AnalysisProcedure_Template "
                        "(ProcedureID, Position, CellID, CellType, SampleID) "
                        "VALUES (:pid, :pos, :cid, :ctype, :sid)"
                    ), {"pid": self.procedure_id, "pos": pos,
                        "cid": int(cid), "ctype": ctype, "sid": sid})
                conn.commit()

            QMessageBox.information(self, "Saved",
                                    "Enrichment template saved successfully.")
            self.accept()

        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))
