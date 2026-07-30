"""
trims_chemical_enrichment_gui.py — TRIMS Chemical Enrichment run details view.

Handles pre-concentration enrichment for non-tritium LSC isotopes (e.g. 35S, 14C).
Three enrichment methods are supported:
  5 = Gravimetric  : EF = (InitVol / FinalVol)
  6 = SpikeRecovery: EF = (InitVol / FinalVol) * (SpikeMeasured / SpikeAdded)
  7 = Volumetric   : EF = (InitVol / FinalVol)   [volume-only, no carrier tracking]

Layout follows TrimsElectrolysisDetailsWindow conventions:
  • Header QGroupBox with run-level metadata
  • QTableView with NumericDelegate for per-sample measurements
  • Column-unlock-on-header-double-click edit discipline
  • Row-level auto-persist on cell change
"""
from __future__ import annotations

import getpass
import logging
import math
from datetime import datetime
from typing import List, Optional, Set, Tuple

from PyQt5.QtCore import Qt, QEvent, QTimer
from PyQt5.QtGui import (
    QBrush, QColor, QDoubleValidator, QStandardItem, QStandardItemModel,
)
from PyQt5.QtWidgets import (
    QAbstractItemDelegate, QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit,
    QDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QStyle, QStyledItemDelegate, QTableView, QTextEdit, QVBoxLayout,
    QWidget,
)
from sqlalchemy import text

from db_core import db_manager
from shared_utils import check_employee_privilege, set_status
from gui_utils import show_message

# ---------------------------------------------------------------------------
# EF Method constants
# ---------------------------------------------------------------------------
METH_GRAVIMETRIC    = 5   # EF = InitVol / FinalVol
METH_SPIKE_RECOVERY = 6   # EF = (InitVol / FinalVol) * recovery
METH_VOLUMETRIC     = 7   # EF = InitVol / FinalVol  (no carrier)

METH_LABELS = {
    METH_GRAVIMETRIC:    "Gravimetric",
    METH_SPIKE_RECOVERY: "Spike Recovery",
    METH_VOLUMETRIC:     "Volumetric",
}

# Analysis status codes (shared with electrolysis module)
STATUS_BEING_ENRICHED = 4
STATUS_ENRICHED       = 5
STATUS_FAILED         = -9

STATUS_OPTIONS: List[Tuple[int, str]] = [
    (STATUS_BEING_ENRICHED, "Being Enriched"),
    (STATUS_ENRICHED,       "Enriched"),
    (STATUS_FAILED,         "Failed"),
]
STATUS_DESC = {code: desc for code, desc in STATUS_OPTIONS}


# ---------------------------------------------------------------------------
# Delegates
# ---------------------------------------------------------------------------

class _HighlightDelegate(QStyledItemDelegate):
    """Base: blue tint on target columns + focus border when editable."""
    _BG   = QColor("#e3f2fd")
    _FOCUS = QColor("#0078d7")

    def __init__(self, parent=None, target_columns=None):
        super().__init__(parent)
        self.target_columns: Set[int] = set(target_columns or [])

    def set_target_columns(self, cols):
        self.target_columns = set(cols)

    def paint(self, painter, option, index):
        item_bg = index.data(Qt.BackgroundRole)
        painter.save()
        if item_bg and item_bg != QBrush():
            painter.fillRect(option.rect, item_bg)
        elif index.column() in self.target_columns:
            painter.fillRect(option.rect, self._BG)
        painter.restore()
        super().paint(painter, option, index)
        if (option.state & QStyle.State_HasFocus) and (index.flags() & Qt.ItemIsEditable):
            painter.save()
            pen = painter.pen()
            pen.setColor(self._FOCUS)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRect(option.rect.adjusted(1, 1, -1, -1))
            painter.restore()


class NumericDelegate(_HighlightDelegate):
    """Numeric inline editor; Enter advances to next row in same column."""

    def __init__(self, parent=None, decimals=2):
        super().__init__(parent)
        self.decimals = decimals

    def createEditor(self, parent, option, index):
        if index.column() not in self.target_columns:
            return None
        ed = QLineEdit(parent)
        ed.setValidator(QDoubleValidator(-1e15, 1e15, self.decimals, ed))
        ed.installEventFilter(self)
        return ed

    def setEditorData(self, editor, index):
        editor.setText(index.data(Qt.EditRole) or "")
        editor.selectAll()

    def setModelData(self, editor, model, index):
        txt = editor.text().strip()
        if txt:
            try:
                val = float(txt)
                model.setData(index, f"{val:.{self.decimals}f}", Qt.EditRole)
                model.setData(index, val, Qt.UserRole + 1)
                return
            except ValueError:
                pass
        model.setData(index, "", Qt.EditRole)
        model.setData(index, None, Qt.UserRole + 1)

    def eventFilter(self, editor, event):
        if event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.commitData.emit(editor)
                view = self.parent()
                if isinstance(view, QTableView):
                    cur = view.currentIndex()
                    nxt = view.model().index((cur.row() + 1) % view.model().rowCount(), cur.column())
                    view.setCurrentIndex(nxt)
                    view.scrollTo(nxt)
                self.closeEditor.emit(editor, QAbstractItemDelegate.EditNextItem)
                return True
            if event.key() == Qt.Key_Escape:
                self.closeEditor.emit(editor, QAbstractItemDelegate.RevertModelCache)
                return True
        return super().eventFilter(editor, event)


class EFColorDelegate(QStyledItemDelegate):
    """Paints EF cell: green ≥ target, yellow ≥ 0.5 × target, red below, gray if N/A."""
    _GREEN  = QColor("#c8e6c9")
    _YELLOW = QColor("#fff9c4")
    _RED    = QColor("#ffcdd2")
    _GRAY   = QColor("#f9f9f9")

    def __init__(self, parent=None, target_ef=1.0):
        super().__init__(parent)
        self.target_ef = float(target_ef or 1.0)

    def paint(self, painter, option, index):
        val = None
        try:
            s = str(index.data(Qt.DisplayRole) or "").strip()
            val = float(s) if s else None
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        if val is not None and self.target_ef > 1.0:
            bg = self._GREEN if val >= self.target_ef else (
                 self._YELLOW if val >= 0.5 * self.target_ef else self._RED)
        else:
            bg = self._GRAY
        painter.save()
        painter.fillRect(option.rect, QBrush(bg))
        painter.restore()
        super().paint(painter, option, index)


class StatusComboDelegate(QStyledItemDelegate):
    """ComboBox delegate for Analysis.Status."""

    def __init__(self, parent=None, options=None):
        super().__init__(parent)
        self.options: List[Tuple[int, str]] = options or STATUS_OPTIONS

    def createEditor(self, parent, option, index):
        cb = QComboBox(parent)
        for code, desc in self.options:
            cb.addItem(desc, code)
        cb.activated.connect(lambda _: self.commitData.emit(cb))
        return cb

    def setEditorData(self, editor, index):
        code = index.data(Qt.UserRole) or STATUS_BEING_ENRICHED
        i = editor.findData(code)
        editor.setCurrentIndex(max(0, i))

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.DisplayRole)
        model.setData(index, editor.currentData(), Qt.UserRole)


# ---------------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------------

class ChemicalEnrichmentWindow(QDialog):
    """View / edit a Chemical Enrichment run (one run = one isotope batch)."""

    # ---- Column indices ----
    COL_AID        = 0
    COL_SAMPLE     = 1
    COL_TYPE       = 2
    COL_INIT_VOL   = 3
    COL_INIT_MASS  = 4
    COL_FINAL_VOL  = 5
    COL_FINAL_MASS = 6
    COL_CARRIER    = 7
    COL_SPK_ADD    = 8
    COL_SPK_MEAS   = 9
    COL_RECOVERY   = 10   # computed, read-only
    COL_EF         = 11   # computed, read-only
    COL_EF_UNC     = 12   # computed, read-only
    COL_CANCEL     = 13
    COL_STATUS     = 14
    COL_REMARKS    = 15

    _HEADERS = [
        "AnalysisID", "Sample", "Type",
        "Init Vol (mL)", "Init Mass (g)",
        "Final Vol (mL)", "Final Mass (g)",
        "Carrier (g)",
        "Spike Added (DPM)", "Spike Meas. (DPM)",
        "Recovery", "EF", "EF Unc",
        "Cancel?", "Status", "Remarks",
    ]

    # Columns that are always read-only
    _RO_COLS = {COL_AID, COL_SAMPLE, COL_TYPE, COL_RECOVERY, COL_EF, COL_EF_UNC}

    # Numeric editable candidates (unlocked one at a time by header double-click)
    _NUMERIC_COLS = {
        COL_INIT_VOL, COL_INIT_MASS, COL_FINAL_VOL, COL_FINAL_MASS,
        COL_CARRIER, COL_SPK_ADD, COL_SPK_MEAS,
    }

    def __init__(self, run_id: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.run_id = run_id
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)
        self.setWindowTitle(f"Chemical Enrichment — Run {run_id}")
        self.resize(1400, 780)

        self.has_write_priv  = False
        self.is_editing      = False
        self.active_edit_col: Optional[int] = None
        self._updating_model = False
        self._ef_method      = METH_GRAVIMETRIC   # default; loaded from run header
        self._measurable_id  = 0
        self._measurable_name = ""

        # ---- Widgets declared before _init_ui ----
        self.btnEdit  = QPushButton("Edit"); self.btnEdit.setCheckable(True); self.btnEdit.setAutoDefault(False)
        self.btnClose = QPushButton("Close"); self.btnClose.setAutoDefault(False)

        self.txtRunID      = QLineEdit()
        self.cmbIsotope    = QComboBox()
        self.cmbMethod     = QComboBox()
        self.dtDate        = QDateTimeEdit()
        self.chkFinished   = QCheckBox("Finished?")
        self.cmbTechnician = QComboBox()
        self.txtRemarks    = QTextEdit()
        self.status_label  = QLabel("Ready")

        # ---- Table ----
        self.table = QTableView()
        self.model = QStandardItemModel()
        self.table.setModel(self.model)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(False)

        # ---- Delegates ----
        self.num_delegate    = NumericDelegate(self.table, decimals=2)
        self.ef_delegate     = EFColorDelegate(self.table)
        self.status_delegate = StatusComboDelegate(self.table)

        self._init_ui()
        try:
            self._check_privileges()
            self._load_lookups()
            self._load_header()
            self._load_details()
            self._apply_read_only_state()
        except Exception as exc:
            logging.error(f"ChemicalEnrichmentWindow init: {exc}")
            self.status_label.setText(f"Error: {exc}")

    # ------------------------------------------------------------------
    # UI scaffold
    # ------------------------------------------------------------------

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        top_bar = QHBoxLayout()
        top_bar.addStretch()
        top_bar.addWidget(self.btnEdit)
        top_bar.addWidget(self.btnClose)
        root.addLayout(top_bar)

        self._build_header_form()
        root.addWidget(self.header_group)
        self._apply_table_styles()
        root.addWidget(self.table, 1)
        root.addWidget(self.status_label)

        self.btnEdit.toggled.connect(self._toggle_edit_mode)
        self.btnClose.clicked.connect(self.accept)
        self.model.itemChanged.connect(self._on_item_changed)
        self.table.horizontalHeader().sectionDoubleClicked.connect(self._on_header_dblclick)

    def _build_header_form(self):
        self.header_group = QGroupBox("Run Metadata")
        self.header_group.setStyleSheet("""
            QGroupBox { background:#FAFAFC; border:1px solid #D9D9E3; border-radius:8px;
                        margin-top:8px; font-weight:600; padding-top:10px; }
            QGroupBox::title { subcontrol-origin:margin; left:10px; padding:0 6px;
                               color:#2D2D33; font-size:13px; }
            QLabel { color:#3A3A44; font-size:12.5px; font-weight:500; }
            QLineEdit, QComboBox, QTextEdit, QDateTimeEdit {
                background:#FFF; border:1px solid #D0D0DD; border-radius:6px;
                padding:4px 6px; font-size:12.5px; }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QDateTimeEdit:focus {
                border:1px solid #4C82FF; }
        """)
        grid = QGridLayout()
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1); grid.setColumnStretch(3, 1); grid.setColumnStretch(5, 1)

        def lbl(t):
            w = QLabel(t); w.setAlignment(Qt.AlignRight | Qt.AlignVCenter); return w

        self.txtRunID.setReadOnly(True)
        self.txtRunID.setFixedWidth(80)
        self.txtRunID.setStyleSheet("background:#F2F3F7; font-weight:600;")
        self.dtDate.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.cmbMethod.addItems(["— select —"] + list(METH_LABELS.values()))
        self.txtRemarks.setMaximumHeight(48)

        row = 0
        grid.addWidget(lbl("Run ID:"),    row, 0); grid.addWidget(self.txtRunID,      row, 1)
        grid.addWidget(lbl("Isotope:"),   row, 2); grid.addWidget(self.cmbIsotope,    row, 3)
        grid.addWidget(lbl("Method:"),    row, 4); grid.addWidget(self.cmbMethod,     row, 5)
        row += 1
        h_date = QHBoxLayout(); h_date.addWidget(self.dtDate); h_date.addWidget(self.chkFinished)
        grid.addWidget(lbl("Date:"),       row, 0); grid.addLayout(h_date,            row, 1)
        grid.addWidget(lbl("Technician:"), row, 2); grid.addWidget(self.cmbTechnician, row, 3)
        row += 1
        grid.addWidget(lbl("Remarks:"),   row, 0); grid.addWidget(self.txtRemarks,    row, 1, 1, 5)

        self.header_group.setLayout(grid)

    def _apply_table_styles(self):
        self.table.setStyleSheet("""
            QTableView { border:1px solid #d0d0d0; background:white;
                         gridline-color:#cccccc;
                         selection-background-color:#DDEEFF; selection-color:#000; }
            QTableView::item { padding:4px; border:none; }
            QHeaderView::section { background:#f0f0f0; font-weight:bold;
                                   border:1px solid #d0d0d0; padding:4px; }
        """)
        self.table.setAlternatingRowColors(False)
        self.table.setWordWrap(False)

    # ------------------------------------------------------------------
    # Privilege & lookups
    # ------------------------------------------------------------------

    def _check_privileges(self):
        try:
            self.has_write_priv = check_employee_privilege(getpass.getuser(), "AccessEnrichment")
        except Exception:
            self.has_write_priv = False

    def _load_lookups(self):
        with db_manager.get_connection() as conn:
            self.cmbTechnician.addItem("", None)
            for r in conn.execute(text(
                "SELECT EmployeeID, LastName, FirstMiddleName FROM Employee ORDER BY LastName"
            )):
                self.cmbTechnician.addItem(f"{r.LastName}, {r.FirstMiddleName}", r.EmployeeID)

            # Measurables that are LSC-relevant (non-tritium handled here)
            self.cmbIsotope.addItem("", None)
            for r in conn.execute(text(
                "SELECT AnalyteID AS MeasurableID, AnalyteName AS MeasurableName FROM Analytes "
                "WHERE AnalyteID <> 3 ORDER BY AnalyteName"
            )):
                self.cmbIsotope.addItem(r.MeasurableName, r.MeasurableID)

    # ------------------------------------------------------------------
    # Header load / save
    # ------------------------------------------------------------------

    def _load_header(self):
        with db_manager.get_connection() as conn:
            row = conn.execute(text("""
                SELECT r.RunID, r.MeasurableID, r.EnrichmentMethod,
                       r.RunDate, r.IsFinished, r.TechnicianID, r.Remarks,
                       m.AnalyteName AS MeasurableName
                FROM TRIMS.ChemEnrRun r
                LEFT JOIN Analytes m ON m.AnalyteID = r.MeasurableID
                WHERE r.RunID = :rid
            """), {"rid": self.run_id}).fetchone()

        if not row:
            set_status(self.status_label, "Run not found.", "error")
            return

        self.txtRunID.setText(str(row.RunID))
        self._measurable_id   = row.MeasurableID or 0
        self._measurable_name = row.MeasurableName or ""
        self._ef_method       = int(row.EnrichmentMethod or METH_GRAVIMETRIC)

        self._set_combo(self.cmbIsotope,  row.MeasurableID)
        self._set_method_combo(self._ef_method)
        if row.RunDate:
            self.dtDate.setDateTime(row.RunDate)
        self.chkFinished.setChecked(bool(row.IsFinished))
        self._set_combo(self.cmbTechnician, row.TechnicianID)
        self.txtRemarks.setPlainText(row.Remarks or "")
        self.setWindowTitle(
            f"Chemical Enrichment — Run {self.run_id}"
            + (f" [{self._measurable_name}]" if self._measurable_name else "")
        )

    def _save_header(self):
        if not self.has_write_priv:
            return
        is_finished = self.chkFinished.isChecked()
        meth_code   = self._selected_method_code()
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE TRIMS.ChemEnrRun
                    SET MeasurableID=:mid, EnrichmentMethod=:meth,
                        RunDate=:dt, IsFinished=:fin,
                        TechnicianID=:tech, Remarks=:rem,
                        ModifDateStamp=:now, ModifUserStamp=:usr
                    WHERE RunID=:rid
                """), {
                    "mid":  self.cmbIsotope.currentData(),
                    "meth": meth_code,
                    "dt":   self.dtDate.dateTime().toPyDateTime(),
                    "fin":  is_finished,
                    "tech": self.cmbTechnician.currentData(),
                    "rem":  self.txtRemarks.toPlainText()[:255],
                    "now":  datetime.now(),
                    "usr":  getpass.getuser(),
                    "rid":  self.run_id,
                })
                conn.commit()
            self._ef_method = meth_code
            if is_finished:
                self._propagate_status()
            set_status(self.status_label, "Metadata saved.", "success")
        except Exception as exc:
            logging.error(f"_save_header: {exc}")
            set_status(self.status_label, f"Save error: {exc}", "error")

    def _propagate_status(self):
        """Set Analysis.Status for every row based on IsIgnored + EF presence."""
        try:
            with db_manager.get_connection() as conn:
                trans = conn.begin()
                try:
                    for r in range(self.model.rowCount()):
                        aid = int(self.model.item(r, self.COL_AID).text())
                        is_ignored = (self.model.item(r, self.COL_CANCEL).checkState() == Qt.Checked)
                        ef_text = self.model.item(r, self.COL_EF).text().strip()
                        has_ef  = bool(ef_text and float(ef_text) > 0)

                        if is_ignored:
                            new_st = STATUS_FAILED
                        elif has_ef:
                            new_st = STATUS_ENRICHED
                        else:
                            new_st = STATUS_BEING_ENRICHED

                        conn.execute(text(
                            "UPDATE Analysis SET Status=:st WHERE AnalysisID=:aid"
                        ), {"st": new_st, "aid": aid})

                        st_item = self.model.item(r, self.COL_STATUS)
                        st_item.setText(STATUS_DESC.get(new_st, str(new_st)))
                        st_item.setData(new_st, Qt.UserRole)
                    trans.commit()
                    set_status(self.status_label, "Analysis statuses updated.", "success")
                except Exception:
                    trans.rollback()
                    raise
        except Exception as exc:
            logging.error(f"_propagate_status: {exc}")
            set_status(self.status_label, f"Status propagation failed: {exc}", "error")

    # ------------------------------------------------------------------
    # Detail rows load
    # ------------------------------------------------------------------

    def _load_details(self):
        self._updating_model = True
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT ce.ChemEnrID, ce.AnalysisID, ce.MeasurableID,
                           ce.InitialVolumeML, ce.InitialMassG,
                           ce.FinalVolumeML,   ce.FinalMassG,
                           ce.CarrierMassG,
                           ce.SpikeAddedDPM,   ce.SpikeMeasuredDPM,
                           ce.RecoveryFraction,
                           ce.EnrichmentFactor, ce.EnrichmentFactorUnc,
                           ce.EnrichmentMethod,
                           ce.IsIgnored, ce.Remarks,
                           a.Status AS AStatus,
                           s.sName, s.Prefix, a.SampleID AS SID,
                           s.SampleType AS ActualSampleType
                    FROM TRIMS.ChemicalEnrichment ce
                    JOIN Analysis a  ON a.AnalysisID  = ce.AnalysisID
                    JOIN Sample   s  ON s.SampleID    = a.SampleID
                                    AND s.Prefix      = a.Prefix
                    WHERE ce.RunID = :rid
                    ORDER BY ce.ChemEnrID
                """), {"rid": self.run_id}).fetchall()

            self.model.clear()
            self.model.setHorizontalHeaderLabels(self._HEADERS)

            brush_ro = QBrush(QColor("#f9f9f9"))

            for r in rows:
                aid        = r.AnalysisID
                ef         = r.EnrichmentFactor
                ef_unc     = r.EnrichmentFactorUnc
                recovery   = r.RecoveryFraction
                a_status   = int(r.AStatus or STATUS_BEING_ENRICHED)
                is_ignored = bool(r.IsIgnored)

                items = [
                    QStandardItem(str(aid)),
                    QStandardItem(f"{r.Prefix}-{r.SID} {r.sName}"),
                    QStandardItem(str(r.ActualSampleType)),
                    QStandardItem(self._fmt(r.InitialVolumeML)),
                    QStandardItem(self._fmt(r.InitialMassG)),
                    QStandardItem(self._fmt(r.FinalVolumeML)),
                    QStandardItem(self._fmt(r.FinalMassG)),
                    QStandardItem(self._fmt(r.CarrierMassG)),
                    QStandardItem(self._fmt(r.SpikeAddedDPM,  decimals=1)),
                    QStandardItem(self._fmt(r.SpikeMeasuredDPM, decimals=1)),
                    QStandardItem(self._fmt(recovery, decimals=4)),   # Recovery (RO computed)
                    QStandardItem(self._fmt(ef,        decimals=4)),   # EF       (RO computed)
                    QStandardItem(self._fmt(ef_unc,    decimals=4)),   # EF Unc   (RO computed)
                    QStandardItem(""),                                 # Cancel?
                    QStandardItem(STATUS_DESC.get(a_status, str(a_status))),  # Status
                    QStandardItem(r.Remarks or ""),
                ]

                # Store keys in UserRole
                items[self.COL_AID].setData(aid, Qt.UserRole)
                items[self.COL_STATUS].setData(a_status, Qt.UserRole)

                # Mark read-only columns
                for ci in self._RO_COLS:
                    items[ci].setEditable(False)
                    items[ci].setBackground(brush_ro)
                    items[ci].setFlags(items[ci].flags() & ~Qt.ItemIsEditable)

                # Cancel? checkbox
                ci_item = items[self.COL_CANCEL]
                ci_item.setCheckable(True)
                ci_item.setCheckState(Qt.Checked if is_ignored else Qt.Unchecked)
                ci_item.setFlags(
                    (ci_item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    & ~Qt.ItemIsEditable
                )

                self.model.appendRow(items)

            # Assign static delegates
            self.table.setItemDelegateForColumn(self.COL_EF,     self.ef_delegate)
            self.table.setItemDelegateForColumn(self.COL_STATUS,  self.status_delegate)

            self.table.resizeColumnsToContents()
            self.table.horizontalHeader().setSectionResizeMode(self.COL_REMARKS, QHeaderView.Stretch)

            # Hide spike columns when method is not SpikeRecovery
            self._update_column_visibility()

        finally:
            self._updating_model = False
        self._update_editable_flags()

    # ------------------------------------------------------------------
    # Computed EF
    # ------------------------------------------------------------------

    def _recompute_ef(self, row: int):
        """Recompute Recovery, EF, EF Unc from current row values and write to model."""
        def g(col) -> Optional[float]:
            try:
                t = self.model.item(row, col).text().strip()
                return float(t) if t else None
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); return None

        init_vol   = g(self.COL_INIT_VOL)
        final_vol  = g(self.COL_FINAL_VOL)
        spk_add    = g(self.COL_SPK_ADD)
        spk_meas   = g(self.COL_SPK_MEAS)

        # --- Recovery ---
        recovery = None
        recovery_unc = None
        if self._ef_method == METH_SPIKE_RECOVERY and spk_add and spk_meas and spk_add > 0:
            recovery = spk_meas / spk_add
            # Relative uncertainty: sqrt((unc_meas/meas)^2 + (unc_add/add)^2)
            # We don't have individual uncertainties here, so we propagate as 1%
            recovery_unc = recovery * math.sqrt(2) * 0.01

        # --- EF ---
        ef = None
        ef_unc = None
        if init_vol and final_vol and final_vol > 0:
            vol_ratio = init_vol / final_vol
            r_eff = recovery if recovery is not None else 1.0
            ef    = vol_ratio * r_eff
            # Propagated relative uncertainty (volume ratio only — 0.5% each)
            vol_rel_unc = math.sqrt(2) * 0.005
            if recovery is not None and recovery > 0:
                rec_rel_unc = recovery_unc / recovery if recovery_unc else 0
                ef_unc = ef * math.sqrt(vol_rel_unc**2 + rec_rel_unc**2)
            else:
                ef_unc = ef * vol_rel_unc

        self._updating_model = True
        try:
            self.model.item(row, self.COL_RECOVERY).setText(
                f"{recovery:.4f}" if recovery is not None else "")
            self.model.item(row, self.COL_EF).setText(
                f"{ef:.4f}" if ef is not None else "")
            self.model.item(row, self.COL_EF_UNC).setText(
                f"{ef_unc:.4f}" if ef_unc is not None else "")
        finally:
            self._updating_model = False

    # ------------------------------------------------------------------
    # Edit mode
    # ------------------------------------------------------------------

    def _toggle_edit_mode(self, checked: bool):
        if checked and not self.has_write_priv:
            self.btnEdit.setChecked(False)
            show_message(self, "No permission", "You do not have write access to Chemical Enrichment.", "warning")
            return
        self.is_editing = checked
        if checked:
            self.btnEdit.setText("Stop Edit")
            self.table.setEditTriggers(QAbstractItemView.AllEditTriggers)
            self.active_edit_col = None
            set_status(self.status_label,
                       "Edit Mode — double-click a column header to unlock that field.", "processing")
        else:
            self._save_header()
            self.btnEdit.setText("Edit")
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.active_edit_col = None
            self._apply_read_only_state()
            set_status(self.status_label, "Read Only", "neutral")
        self._update_editable_flags()

    def _on_header_dblclick(self, col: int):
        if not self.is_editing or col not in self._NUMERIC_COLS:
            return
        self.active_edit_col = col
        self.num_delegate.set_target_columns([col])
        self.table.setItemDelegateForColumn(col, self.num_delegate)
        self._update_editable_flags()
        set_status(
            self.status_label,
            f"Active column: {self._HEADERS[col]}  — Enter to advance rows.",
            "processing",
        )

    def _update_editable_flags(self):
        self.model.blockSignals(True)
        try:
            for r in range(self.model.rowCount()):
                for c in range(self.model.columnCount()):
                    it = self.model.item(r, c)
                    if c in self._RO_COLS:
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    elif c == self.COL_CANCEL:
                        if self.is_editing:
                            it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                        else:
                            it.setFlags(it.flags() & ~Qt.ItemIsUserCheckable & ~Qt.ItemIsEditable)
                    elif c == self.COL_STATUS:
                        it.setFlags(
                            (it.flags() | Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                            if self.is_editing
                            else (it.flags() & ~Qt.ItemIsEditable)
                        )
                    elif c == self.COL_REMARKS:
                        it.setFlags(
                            (it.flags() | Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                            if self.is_editing
                            else (it.flags() & ~Qt.ItemIsEditable)
                        )
                    elif c in self._NUMERIC_COLS:
                        if self.is_editing and c == self.active_edit_col:
                            it.setFlags(it.flags() | Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                        else:
                            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
        finally:
            self.model.blockSignals(False)
        self.table.viewport().update()

    def _apply_read_only_state(self):
        for w in [self.dtDate, self.txtRemarks]:
            if hasattr(w, "setReadOnly"):
                w.setReadOnly(True)
        self.chkFinished.setEnabled(False)
        self.cmbTechnician.setEnabled(False)
        self.cmbIsotope.setEnabled(False)
        self.cmbMethod.setEnabled(False)

    def _apply_edit_state(self):
        for w in [self.dtDate, self.txtRemarks]:
            if hasattr(w, "setReadOnly"):
                w.setReadOnly(False)
        self.chkFinished.setEnabled(True)
        self.cmbTechnician.setEnabled(True)
        self.cmbIsotope.setEnabled(True)
        self.cmbMethod.setEnabled(True)

    # ------------------------------------------------------------------
    # Item changed & row persistence
    # ------------------------------------------------------------------

    def _on_item_changed(self, item: QStandardItem):
        if not self.is_editing or self._updating_model:
            return
        self._updating_model = True
        try:
            r, c = item.row(), item.column()
            if c in self._NUMERIC_COLS:
                self._recompute_ef(r)
                self._save_row_to_db(r)
            elif c == self.COL_CANCEL:
                self._save_row_to_db(r)
                if item.checkState() == Qt.Checked:
                    idx = self.model.index(r, self.COL_REMARKS)
                    self.table.setCurrentIndex(idx)
                    if self.is_editing:
                        QTimer.singleShot(0, lambda: self.table.edit(idx))
            elif c == self.COL_REMARKS:
                self._save_row_to_db(r)
            # COL_STATUS changes are model-only; persisted on _propagate_status
        finally:
            self._updating_model = False

    def _save_row_to_db(self, row: int):
        try:
            aid = self.model.item(row, self.COL_AID).data(Qt.UserRole)

            def g(col) -> Optional[float]:
                try:
                    t = self.model.item(row, col).text().strip()
                    return float(t) if t else None
                except Exception as e:

                    logging.warning(f"Exception caught: {e}"); return None

            is_ignored = (self.model.item(row, self.COL_CANCEL).checkState() == Qt.Checked)

            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE TRIMS.ChemicalEnrichment
                    SET InitialVolumeML=:iv,  InitialMassG=:im,
                        FinalVolumeML=:fv,    FinalMassG=:fm,
                        CarrierMassG=:car,
                        SpikeAddedDPM=:sa,    SpikeMeasuredDPM=:sm,
                        RecoveryFraction=:rec,
                        EnrichmentFactor=:ef, EnrichmentFactorUnc=:efu,
                        EnrichmentMethod=:meth,
                        IsIgnored=:ign,
                        Remarks=:rem,
                        ModifDateStamp=:now,  ModifUserStamp=:usr
                    WHERE RunID=:rid AND AnalysisID=:aid
                """), {
                    "iv":  g(self.COL_INIT_VOL),  "im": g(self.COL_INIT_MASS),
                    "fv":  g(self.COL_FINAL_VOL),  "fm": g(self.COL_FINAL_MASS),
                    "car": g(self.COL_CARRIER),
                    "sa":  g(self.COL_SPK_ADD),    "sm": g(self.COL_SPK_MEAS),
                    "rec": g(self.COL_RECOVERY),
                    "ef":  g(self.COL_EF),          "efu": g(self.COL_EF_UNC),
                    "meth": self._ef_method,
                    "ign": is_ignored,
                    "rem": self.model.item(row, self.COL_REMARKS).text(),
                    "now": datetime.now(), "usr": getpass.getuser(),
                    "rid": self.run_id,   "aid": aid,
                })
                conn.commit()
            set_status(self.status_label, f"Saved Analysis {aid}.", "success")
        except Exception as exc:
            logging.error(f"_save_row_to_db: {exc}")
            set_status(self.status_label, f"Row save error: {exc}", "error")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fmt(self, val, decimals: int = 2) -> str:
        if val is None:
            return ""
        try:
            return f"{float(val):.{decimals}f}"
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return ""

    def _set_combo(self, cmb: QComboBox, val):
        if val is None:
            cmb.setCurrentIndex(0)
            return
        i = cmb.findData(val)
        cmb.setCurrentIndex(max(0, i))

    def _set_method_combo(self, code: int):
        label = METH_LABELS.get(code, "")
        i = self.cmbMethod.findText(label)
        self.cmbMethod.setCurrentIndex(max(0, i))

    def _selected_method_code(self) -> int:
        label = self.cmbMethod.currentText()
        return {v: k for k, v in METH_LABELS.items()}.get(label, METH_GRAVIMETRIC)

    def _update_column_visibility(self):
        """Show spike columns only when EF method is SpikeRecovery."""
        is_spike = (self._ef_method == METH_SPIKE_RECOVERY)
        for col in (self.COL_SPK_ADD, self.COL_SPK_MEAS, self.COL_RECOVERY):
            self.table.setColumnHidden(col, not is_spike)


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from PyQt5.QtWidgets import QApplication
    app = QApplication(sys.argv)
    w = ChemicalEnrichmentWindow(run_id=1)
    w.show()
    sys.exit(app.exec_())
