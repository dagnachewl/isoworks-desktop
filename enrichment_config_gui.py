"""
enrichment_template_editor_gui.py
==================================
Combines enrichment_template_editor_gui + enrichment_config_gui into one dialog.

Layout
------
  Left  | All EnrichmentProcedure parameters
        |   Spike ID + count, Dead Water ID + count, Lab Moisture count,
        |   Deuterium flag, Starting/Target water mass, Na2O2 mass, Ampere Hours
        |   Separator
        |   Cell Count + Start Cell ID  (preview controls only)
        |   [Preview Tray] button + colour legend

  Right | Visual-only two-row scrollable tray map.
        |   Never persisted. Enrichment positions rotate at runtime.

Save  -> upsert EnrichmentProcedure only.
         AnalysisProcedure_Template is NOT touched.
"""
import logging
import getpass
from datetime import datetime

from PyQt5.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QLineEdit, QComboBox, QSpinBox, QCheckBox, QPushButton,
    QScrollArea, QFrame, QGridLayout, QDialogButtonBox, QMessageBox,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui  import QFont

from db_core    import db_manager
from sqlalchemy import text

CELL_UNKNOWN   =  0
CELL_SPIKE     =  3
CELL_DEADWATER =  6
CELL_LOCKED    = -2
CELL_EXCLUDED  = -1

_CELL_META = {
    CELL_UNKNOWN:   ("#ffffff", "#555555", "Unknown",    ""),
    CELL_SPIKE:     ("#66bb6a", "#1b5e20", "Spike",      "Spk"),
    CELL_DEADWATER: ("#005f9e", "#ffffff", "Dead Water", "DW"),
    CELL_LOCKED:    ("#ffcc80", "#7f3f00", "Lab Moist.", "Lab"),
    CELL_EXCLUDED:  ("#a8a8a8", "#505050", "Excluded",   "Exc"),
}
_BORDER = {
    "#ffffff": "#cccccc",
    "#66bb6a": "#338a3e",
    "#005f9e": "#003a6e",
    "#ffcc80": "#e65100",
    "#a8a8a8": "#787878",
}


class _PreviewTile(QFrame):
    W, H = 56, 38
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.W, self.H)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 1); lay.setSpacing(0)
        self._lbl_id   = QLabel()
        self._lbl_id.setAlignment(Qt.AlignHCenter | Qt.AlignBottom)
        self._lbl_id.setFont(QFont("Segoe UI", 7, QFont.Bold))
        self._lbl_type = QLabel()
        self._lbl_type.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self._lbl_type.setFont(QFont("Segoe UI", 6))
        lay.addWidget(self._lbl_id); lay.addWidget(self._lbl_type)

    def paint(self, cell_id, ctype):
        bg, fg, label, short = _CELL_META.get(ctype, ("#ffffff","#555555","Unknown",""))
        border = _BORDER.get(bg, "#909090")
        self._lbl_id.setText(str(cell_id)); self._lbl_type.setText(short)
        self.setStyleSheet(
            f"QFrame {{ background:{bg}; border:1px solid {border}; border-radius:4px; }}"
            f"QLabel {{ color:{fg}; background:transparent; border:none; }}"
        )
        self.setToolTip(f"Cell {cell_id}  -  {label}")


class _TrayPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._inner = QWidget()
        self._grid  = QGridLayout(self._inner)
        self._grid.setSpacing(2); self._grid.setContentsMargins(4, 4, 4, 4)
        self._tiles = []
        scroll = QScrollArea()
        scroll.setWidget(self._inner); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedHeight(_PreviewTile.H * 2 + 4 + 20)
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def rebuild(self, cell_ids, ctypes):
        for t in self._tiles: t.setParent(None)
        self._tiles.clear()
        n = len(cell_ids); split = max(n // 2, 1)
        for i, (cid, ctype) in enumerate(zip(cell_ids, ctypes)):
            tile = _PreviewTile(); tile.paint(cid, ctype); self._tiles.append(tile)
            row = 0 if i < split else 1
            col = i if i < split else i - split
            self._grid.addWidget(tile, row, col)


class EnrichmentTemplateEditorDialog(QDialog):
    """
    Unified enrichment configuration dialog.
    Saves to EnrichmentProcedure only (upsert).
    AnalysisProcedure_Template is never written - no fixed cell layout for
    enrichment runs; spikes, blanks, and dead water positions rotate at runtime.
    """

    def __init__(self, procedure_id, procedure_name, parent=None):
        super().__init__(parent)
        self.procedure_id   = procedure_id
        self.procedure_name = procedure_name
        self.setWindowTitle(f"Enrichment Configuration  -  {procedure_name}")
        self.setMinimumSize(900, 540); self.resize(1060, 600)
        self._build_ui()
        self._load_combos()
        self._load_data()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8); root.setSpacing(10)

        # LEFT panel
        left = QGroupBox(f"Procedure Parameters  -  ID {self.procedure_id}")
        left.setFixedWidth(350)
        lf = QFormLayout(left)
        lf.setSpacing(9); lf.setContentsMargins(12, 18, 12, 12)

        self.cmbSpikeID     = QComboBox()
        self.spnSpikes      = QSpinBox(); self.spnSpikes.setRange(0,50);  self.spnSpikes.setValue(1)
        self.cmbDeadWaterID = QComboBox()
        self.spnDW          = QSpinBox(); self.spnDW.setRange(0,50);      self.spnDW.setValue(1)
        self.spnLabAir      = QSpinBox(); self.spnLabAir.setRange(0,50);  self.spnLabAir.setValue(0)
        self.chkDeuterium   = QCheckBox("Uses Deuterium Method")
        self.txtStartWater  = QLineEdit(); self.txtStartWater.setPlaceholderText("g")
        self.txtTargetWater = QLineEdit(); self.txtTargetWater.setPlaceholderText("mL")
        self.txtNa2O2       = QLineEdit(); self.txtNa2O2.setPlaceholderText("g")
        self.txtAmpereHour  = QLineEdit(); self.txtAmpereHour.setPlaceholderText("Ah")

        lf.addRow("Spike ID:",            self.cmbSpikeID)
        lf.addRow("# Spikes:",            self.spnSpikes)
        lf.addRow("Dead Water ID:",       self.cmbDeadWaterID)
        lf.addRow("# Dead Water:",        self.spnDW)
        lf.addRow("# Lab Moisture:",      self.spnLabAir)
        lf.addRow("",                     self.chkDeuterium)
        lf.addRow("Starting Water (g):",  self.txtStartWater)
        lf.addRow("Target Water (mL):",   self.txtTargetWater)
        lf.addRow("Na₂O₂ (g):", self.txtNa2O2)
        lf.addRow("Ampere Hours:",        self.txtAmpereHour)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("QFrame { color:#ccc; }"); lf.addRow(sep)

        self.spnCellCount = QSpinBox()
        self.spnCellCount.setRange(2, 300); self.spnCellCount.setValue(24)
        self.txtStartCell = QLineEdit("101"); self.txtStartCell.setMaximumWidth(70)
        lf.addRow("Cell count (preview):", self.spnCellCount)
        lf.addRow("Start cell ID:",        self.txtStartCell)

        self.btnPreview = QPushButton("Preview Tray")
        self.btnPreview.setStyleSheet(
            "QPushButton { background:#37474f; color:white; font-weight:600;"
            " border-radius:4px; padding:5px 6px; }"
            "QPushButton:hover { background:#546e7a; }")
        self.btnPreview.clicked.connect(self._preview_tray)
        lf.addRow(self.btnPreview)

        leg_grp = QGroupBox("Legend"); ll = QVBoxLayout(leg_grp)
        ll.setSpacing(3); ll.setContentsMargins(6, 6, 6, 6)
        for ctype in [CELL_SPIKE, CELL_DEADWATER, CELL_LOCKED, CELL_UNKNOWN]:
            bg, fg, label, _ = _CELL_META[ctype]
            sw = QLabel(f"  {label}  ")
            sw.setStyleSheet(
                f"background:{bg}; color:{fg}; border:1px solid #bbb;"
                f" border-radius:3px; padding:1px 5px; font-size:10px;")
            sw.setFixedHeight(18); ll.addWidget(sw)
        lf.addRow(leg_grp)
        root.addWidget(left)

        # RIGHT panel
        right = QVBoxLayout(); right.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("<b>Tray Map Preview</b>"))
        note = QLabel("  Visual only - enrichment positions rotate at runtime")
        note.setStyleSheet("color:#888; font-size:10px;")
        hdr.addWidget(note); hdr.addStretch(); right.addLayout(hdr)

        self.tray = _TrayPreview()
        tray_frame = QFrame()
        tray_frame.setStyleSheet(
            "QFrame { border:1px solid #90aec8; border-radius:3px; background:#f0f6fb; }")
        tfl = QVBoxLayout(tray_frame)
        tfl.setContentsMargins(2,2,2,2); tfl.setSpacing(0); tfl.addWidget(self.tray)
        right.addWidget(tray_frame); right.addStretch(1)

        btn_box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Save).setStyleSheet(
            "QPushButton { background:#2e7d32; color:white; font-weight:600;"
            " border-radius:4px; padding:4px 14px; }"
            "QPushButton:hover { background:#388e3c; }")
        btn_box.accepted.connect(self._save); btn_box.rejected.connect(self.reject)
        right.addWidget(btn_box)
        rw = QWidget(); rw.setLayout(right); root.addWidget(rw, 1)

    def _load_combos(self):
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT R.SampleID, S.sName "
                    "FROM ReferenceControl R "
                    "JOIN Sample S ON R.SampleID = S.SampleID "
                    "WHERE S.SampleType IN (3, 4) "
                    "ORDER BY R.SampleID"
                )).fetchall()
            for cmb in (self.cmbSpikeID, self.cmbDeadWaterID):
                cmb.clear(); cmb.addItem("", None)
                for r in rows: cmb.addItem(f"{r[0]}  -  {r[1]}", r[0])
        except Exception as e:
            logging.warning(f"EnrichmentEditor combo load: {e}")

    def _load_data(self):
        if not self.procedure_id: return
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text(
                    "SELECT SpikeID, DeadWaterID, "
                    "       NumberOfSpike, NumberOfDeadWater, NumberOfLabAir, "
                    "       HasDeuteriumMethod, "
                    "       StartingWaterMass, TargetWaterMass, Na2O2Mass, AmpereHour "
                    "FROM   EnrichmentProcedure "
                    "WHERE  ProcedureID = :pid"
                ), {"pid": self.procedure_id}).fetchone()
            if not row: return
            self._set_combo(self.cmbSpikeID,    row.SpikeID)
            self._set_combo(self.cmbDeadWaterID, row.DeadWaterID)
            self.spnSpikes.setValue( int(row.NumberOfSpike    or 0))
            self.spnDW.setValue(     int(row.NumberOfDeadWater or 0))
            self.spnLabAir.setValue( int(row.NumberOfLabAir   or 0))
            self.chkDeuterium.setChecked(bool(row.HasDeuteriumMethod))
            self.txtStartWater.setText( str(row.StartingWaterMass or ""))
            self.txtTargetWater.setText(str(row.TargetWaterMass   or ""))
            self.txtNa2O2.setText(      str(row.Na2O2Mass         or ""))
            self.txtAmpereHour.setText( str(row.AmpereHour        or ""))
            self._preview_tray()
        except Exception as e:
            logging.warning(f"EnrichmentEditor load: {e}")

    @staticmethod
    def _set_combo(cmb, value):
        if value is None: return
        idx = cmb.findData(value)
        if idx >= 0: cmb.setCurrentIndex(idx)

    def _preview_tray(self):
        n    = self.spnCellCount.value()
        n_sp = self.spnSpikes.value()
        n_dw = self.spnDW.value()
        n_la = self.spnLabAir.value()
        try:    start = int(self.txtStartCell.text())
        except: start = 101
        ids   = [str(start + i) for i in range(n)]
        types = []; rem = n
        for _ in range(min(n_sp, rem)): types.append(CELL_SPIKE);     rem -= 1
        for _ in range(min(n_dw, rem)): types.append(CELL_DEADWATER); rem -= 1
        blanks = min(n_la, rem)
        for _ in range(rem - blanks): types.append(CELL_UNKNOWN)
        for _ in range(blanks):       types.append(CELL_LOCKED)
        self.tray.rebuild(ids, types)

    def _save(self):
        def _f(w):
            try:    return float(w.text().strip())
            except: return None
        now = datetime.now(); user = getpass.getuser()
        data = {
            "pid":  self.procedure_id,
            "sid":  self.cmbSpikeID.currentData(),
            "dwid": self.cmbDeadWaterID.currentData(),
            "nsp":  self.spnSpikes.value(),
            "ndw":  self.spnDW.value(),
            "nla":  self.spnLabAir.value(),
            "deut": self.chkDeuterium.isChecked(),
            "sw":   _f(self.txtStartWater),
            "tw":   _f(self.txtTargetWater),
            "na2":  _f(self.txtNa2O2),
            "ah":   _f(self.txtAmpereHour),
            "now":  now, "user": user,
        }
        try:
            with db_manager.get_connection() as conn:
                exists = conn.execute(
                    text("SELECT 1 FROM EnrichmentProcedure WHERE ProcedureID = :pid"),
                    {"pid": self.procedure_id}).fetchone()
                if exists:
                    conn.execute(text("""
                        UPDATE EnrichmentProcedure SET
                            SpikeID=:sid,            DeadWaterID=:dwid,
                            NumberOfSpike=:nsp,      NumberOfDeadWater=:ndw,
                            NumberOfLabAir=:nla,     HasDeuteriumMethod=:deut,
                            StartingWaterMass=:sw,   TargetWaterMass=:tw,
                            Na2O2Mass=:na2,          AmpereHour=:ah,
                            ModifDateStamp=:now,     ModifUserStamp=:user
                        WHERE ProcedureID=:pid
                    """), data)
                else:
                    conn.execute(text("""
                        INSERT INTO EnrichmentProcedure (
                            ProcedureID,      SpikeID,           DeadWaterID,
                            NumberOfSpike,    NumberOfDeadWater, NumberOfLabAir,
                            HasDeuteriumMethod,
                            StartingWaterMass, TargetWaterMass,
                            Na2O2Mass,         AmpereHour,
                            CreateDateStamp,  CreateUserStamp,
                            ModifDateStamp,   ModifUserStamp
                        ) VALUES (
                            :pid, :sid,  :dwid,
                            :nsp, :ndw,  :nla,
                            :deut,
                            :sw,  :tw,
                            :na2, :ah,
                            :now, :user, :now, :user
                        )
                    """), data)
                conn.commit()
            QMessageBox.information(self, "Saved", "Enrichment configuration saved successfully.")
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))


# Backward-compatibility alias:
# any module still importing EnrichmentConfigDialog from enrichment_config_gui
# continues to work without changes.
EnrichmentConfigDialog = EnrichmentTemplateEditorDialog