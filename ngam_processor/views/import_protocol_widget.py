from __future__ import annotations

import logging
import os
from typing import Dict, Optional

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QLineEdit, QPushButton, QFileDialog, QMessageBox,
    QApplication, QComboBox, QDoubleSpinBox, QProgressDialog, QCheckBox,
    QDialog, QScrollArea, QFormLayout, QTableWidget, QTableWidgetItem, QHeaderView,
    QDateTimeEdit, QGraphicsDropShadowEffect,
)
from PyQt5.QtCore import Qt, QTimer, QDateTime
from PyQt5.QtGui import QColor

from sqlalchemy import text
from db_core import db_manager

from help_browser import make_help_button
from ngam_protocol_parser import lv_to_local_str, merge_sequences
from ngam_supplemental_dialog import SupplementalRunsDialog
from ngam_results_widget import NGAMResultsWidget

from ngam_processor.models.ngam_model import NGAMModel
from ngam_processor.viewmodels.ngam_viewmodel import NGAMViewModel

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Style constants
# ---------------------------------------------------------------------------
# Shared "glass" style for the 5 command buttons (Browse, Clear, Process,
# Import, Close) -- one uniform cool-blue tint instead of the old per-action
# colors, translucent gradient + light border to read as frosted glass.
_BTN_GLASS = (
    "QPushButton {"
    "  background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
    "    stop:0 rgba(120, 190, 255, 190), stop:0.5 rgba(66, 165, 245, 205), stop:1 rgba(25, 118, 210, 215));"
    "  color: white; font-weight: 600;"
    "  border: 1px solid rgba(255, 255, 255, 130);"
    "  border-radius: 8px;"
    "  padding: 6px 16px;"
    "}"
    "QPushButton:hover {"
    "  background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
    "    stop:0 rgba(165, 217, 255, 220), stop:0.5 rgba(93, 188, 255, 230), stop:1 rgba(35, 140, 235, 235));"
    "  border: 1px solid rgba(255, 255, 255, 200);"
    "}"
    "QPushButton:pressed {"
    "  background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
    "    stop:0 rgba(20, 90, 170, 230), stop:1 rgba(10, 60, 130, 230));"
    "  border: 1px solid rgba(255, 255, 255, 90);"
    "}"
    "QPushButton:disabled {"
    "  background: rgba(176, 190, 197, 110);"
    "  color: rgba(255, 255, 255, 140);"
    "  border: 1px solid rgba(255, 255, 255, 60);"
    "}"
)


def _apply_glass_glow(btn: QPushButton) -> None:
    """Soft cool-blue glow behind a glass button -- QSS alone can't do
    box-shadow-style glows in Qt, so this is a QGraphicsDropShadowEffect
    companion to _BTN_GLASS."""
    glow = QGraphicsDropShadowEffect(btn)
    glow.setBlurRadius(18)
    glow.setOffset(0, 2)
    glow.setColor(QColor(66, 165, 245, 160))
    btn.setGraphicsEffect(glow)

_IDMS_DLG_SS = """
    QDialog { background: #FAFBFC; }
    QLabel#iso-label { font-weight: bold; font-size: 12px; color: #1A237E;
                       min-width: 45px; }
    QLabel#hdr-label { font-size: 11px; color: #546E7A; padding: 2px 4px; }
"""

# Common atmospheric isotope ratios used as defaults in IDMS R_sample fields.
_DEFAULT_R_SAMPLE = {
    "22Ne": 0.102, "21Ne": 0.00296, "36Ar": 0.00338,
    "38Ar": 0.000632, "84Kr": 0.566, "86Kr": 0.175,
    "132Xe": 0.269, "134Xe": 0.104, "136Xe": 0.089,
}

_DEVICE_DEFAULT_NUCLIDES = {
    "SMS":     ["3He", "4He"],
    "QMSNe":   ["20Ne", "21Ne", "22Ne"],
    "QMSAr":   ["36Ar", "38Ar", "40Ar"],
    "QMSKrXe": ["82Kr", "83Kr", "84Kr", "86Kr",
                 "129Xe", "131Xe", "132Xe", "134Xe", "136Xe"],
}

_DILUTION_DLG_SS = """
    QDialog { background: #FAFBFC; }
    QLabel#hdr-label { font-size: 11px; color: #546E7A; padding: 2px 4px; }
    QLabel#seq-label { font-weight: bold; color: #1A237E; min-width: 30px; }
"""

_COL_TYPE = {"blank": QColor("#E3F2FD"), "standard": QColor("#F3E5F5"), "sample": QColor("#FFFFFF")}


class _IdmsConfigDialog(QDialog):
    """Modal dialog for configuring per-isotope IDMS parameters.

    Shows a scrollable grid — one row per isotope with:
      Isotope label | Spike Isotope combo | R_sample spinbox | Spike Std text
    """

    def __init__(self, parent, isotopes: list, previous_state: dict = None):
        super().__init__(parent)
        self.setWindowTitle("IDMS Configuration — Isotope Dilution Mass Spectrometry")
        self.setMinimumSize(620, 280)
        self.resize(680, min(520, 40 + 32 * len(isotopes)))
        self.setStyleSheet(_IDMS_DLG_SS)
        self._widgets: dict = {}
        self._isotopes = isotopes
        self._previous_state = previous_state or {}

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # Header instruction
        root.addWidget(QLabel(
            "Configure isotope dilution for each target isotope.  Leave Spike "
            "Iso blank to skip that isotope.  R_sample is the natural atmospheric "
            "ratio of Spike/Target."
        ))

        # Scroll area with grid
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(0, 4, 0, 4)
        grid.setSpacing(4)

        # Column headers
        for col, (text, tip) in enumerate([
            ("Isotope",     "Target isotope"),
            ("Spike Iso",   "Isotope in the enriched spike"),
            ("R_sample",    "Natural (atmospheric) ratio A/B"),
            ("Spike Std",   "Lab ID of the spike standard (e.g. IHSTD-12)"),
        ]):
            hdr = QLabel(text)
            hdr.setObjectName("hdr-label")
            hdr.setToolTip(tip)
            grid.addWidget(hdr, 0, col)

        for row_idx, iso in enumerate(isotopes, start=1):
            prev = self._previous_state.get(iso, {})

            # Isotope label
            lbl = QLabel(iso)
            lbl.setObjectName("iso-label")
            grid.addWidget(lbl, row_idx, 0)

            # Spike isotope combo
            spike_combo = QComboBox()
            spike_combo.addItem("—", "")
            for other_iso in isotopes:
                if other_iso != iso:
                    spike_combo.addItem(other_iso, other_iso)
            prev_spike = prev.get("spike_isotope", "")
            if prev_spike:
                idx = spike_combo.findData(prev_spike)
                if idx >= 0:
                    spike_combo.setCurrentIndex(idx)
            grid.addWidget(spike_combo, row_idx, 1)

            # R_sample
            r_spin = QDoubleSpinBox()
            r_spin.setDecimals(6)
            r_spin.setRange(0.0, 1000.0)
            r_spin.setSingleStep(0.001)
            r_spin.setValue(prev.get("sample_ratio", 0.0))
            default_r = _DEFAULT_R_SAMPLE.get(iso, 0.0)
            if default_r > 0 and r_spin.value() == 0.0:
                r_spin.setValue(default_r)
            grid.addWidget(r_spin, row_idx, 2)

            # Spike standard
            std_edit = QLineEdit()
            std_edit.setPlaceholderText("IHSTD-12")
            std_edit.setText(prev.get("spike_standard", ""))
            grid.addWidget(std_edit, row_idx, 3)

            self._widgets[iso] = (spike_combo, r_spin, std_edit)

        grid.setColumnStretch(3, 1)
        scroll.setWidget(grid_w)
        root.addWidget(scroll, 1)

        # Button row
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("OK")
        ok_btn.setStyleSheet(
            "QPushButton { background: #2E7D32; color: white; font-weight: bold;"
            "  padding: 6px 24px; border-radius: 4px; border: none; }"
            "QPushButton:hover { background: #1B5E20; }"
        )
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(
            "QPushButton { background: #546E7A; color: white; font-weight: bold;"
            "  padding: 6px 24px; border-radius: 4px; border: none; }"
            "QPushButton:hover { background: #37474F; }"
        )
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        root.addLayout(btn_row)

    def get_config(self) -> dict:
        """Return idms_config dict: {target_isotope: {spike_isotope, sample_ratio, spike_standard}}."""
        cfg = {}
        for iso, (combo, r_spin, std_edit) in self._widgets.items():
            spike_iso = combo.currentData()
            sample_ratio = r_spin.value()
            spike_std = std_edit.text().strip()
            if spike_iso and spike_std and sample_ratio > 0:
                cfg[iso] = {
                    "spike_isotope": spike_iso,
                    "sample_ratio": sample_ratio,
                    "spike_standard": spike_std,
                }
        return cfg


class DilutionCalibrationsDialog(QDialog):
    """Read-only table of He/Ne dilution factor calibrations.

    Click "Edit" to unlock editing (requires NGAM_Admin privilege).
    In edit mode: fields become editable, new rows can be added, existing rows
    can be modified, and per-row "Deactivate" buttons are available.
    A global "Save" button validates and persists all changes at once.
    """

    _HEADERS = [
        "Record #", "Equipment", "El", "Factor", "Unc",
        "Valid From", "Valid Until", "Method", "Notes", "Actions",
    ]

    def __init__(self, parent, target_run_id: Optional[int] = None):
        super().__init__(parent)
        self.setWindowTitle("Noble Gas Dilution Calibrations — Read Only")
        self.setMinimumSize(1020, 440)
        self.resize(1120, 520)
        self.target_run_id = target_run_id
        self._editing = False
        self._can_edit = False
        self._original_rows: list = []   # snapshot before editing, for diff

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(12, 12, 12, 12)
        self.layout.setSpacing(8)

        self.layout.addWidget(QLabel(
            "Helium and Neon single-step dilution factor calibrations.\n"
            "Active factors are resolved by instrument and element based on "
            "valid_from / valid_until date ranges."
        ))

        # --- table ---
        self.table = QTableWidget()
        self.table.setColumnCount(len(self._HEADERS))
        self.table.setHorizontalHeaderLabels(self._HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        for _c in range(len(self._HEADERS) - 1):
            if _c != 7:
                self.table.horizontalHeader().setSectionResizeMode(_c, QHeaderView.ResizeToContents)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.verticalHeader().setVisible(False)
        self.layout.addWidget(self.table, 1)

        # --- equipment map ---
        self._eq_map: Dict[Optional[int], str] = {None: "Lab-wide"}
        self._eq_ids: list = []
        try:
            with db_manager.get_connection() as conn:
                for r2 in conn.execute(text(
                    "SELECT equipmentid, equipmentname as name FROM public.equipment "
                    "WHERE categoryid IN (11, 14) ORDER BY name"
                )).fetchall():
                    self._eq_map[r2.equipmentid] = r2.name
                    self._eq_ids.append(r2.equipmentid)
        except Exception as e:
            log.warning(f"Failed to load equipment list: {e}")

        # --- privilege check ---
        try:
            from shared_utils import get_current_user_id, check_employee_privilege, normalize_login_name
            uid = get_current_user_id()
            login = normalize_login_name(uid) if uid else ""
            self._can_edit = check_employee_privilege(login, "NGAM_Admin")
        except Exception:
            self._can_edit = False

        # --- button bar ---
        btn_row = QHBoxLayout()

        self.btn_edit = QPushButton("✎  Edit")
        self.btn_edit.setStyleSheet(
            "QPushButton { background: #1565C0; color: white; font-weight: bold;"
            "  padding: 5px 14px; border-radius: 4px; border: none; }"
            "QPushButton:hover { background: #0D47A1; }"
            "QPushButton:disabled { background: #78909C; }"
        )
        self.btn_edit.setToolTip("Switch to edit mode (NGAM_Admin only)")
        self.btn_edit.clicked.connect(self._enter_edit_mode)
        self.btn_edit.setEnabled(self._can_edit)
        btn_row.addWidget(self.btn_edit)

        self.btn_add = QPushButton("+ Add Row")
        self.btn_add.setStyleSheet(
            "QPushButton { background: #2E7D32; color: white; font-weight: bold;"
            "  padding: 5px 14px; border-radius: 4px; border: none; }"
            "QPushButton:hover { background: #1B5E20; }"
        )
        self.btn_add.clicked.connect(self._add_empty_row)
        self.btn_add.setVisible(False)
        btn_row.addWidget(self.btn_add)

        self.btn_save_all = QPushButton("✓  Save All")
        self.btn_save_all.setStyleSheet(
            "QPushButton { background: #E65100; color: white; font-weight: bold;"
            "  padding: 5px 14px; border-radius: 4px; border: none; }"
            "QPushButton:hover { background: #BF360C; }"
        )
        self.btn_save_all.setToolTip("Validate and save all changes at once")
        self.btn_save_all.clicked.connect(self._save_all)
        self.btn_save_all.setVisible(False)
        btn_row.addWidget(self.btn_save_all)

        self.btn_cancel_edit = QPushButton("Cancel")
        self.btn_cancel_edit.setStyleSheet(
            "QPushButton { background: #546E7A; color: white; font-weight: bold;"
            "  padding: 5px 14px; border-radius: 4px; border: none; }"
            "QPushButton:hover { background: #37474F; }"
        )
        self.btn_cancel_edit.setToolTip("Discard changes and return to read-only mode")
        self.btn_cancel_edit.clicked.connect(self._cancel_edit)
        self.btn_cancel_edit.setVisible(False)
        btn_row.addWidget(self.btn_cancel_edit)

        btn_row.addStretch(1)

        self.btn_close2 = QPushButton("Close")
        self.btn_close2.setStyleSheet(
            "QPushButton { background: #546E7A; color: white; font-weight: bold;"
            "  padding: 5px 18px; border-radius: 4px; border: none; }"
            "QPushButton:hover { background: #37474F; }"
        )
        self.btn_close2.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_close2)
        self.layout.addLayout(btn_row)

        self._load_calibrations()
        self.finished.connect(self._on_closed)

    def _on_closed(self, _result):
        p = self.parent()
        if p and hasattr(p, "target_run_id") and p.target_run_id:
            import ngam_processor.database.ngam_repository as nr
            try:
                p._db_dilution_factors = nr.build_dilution_info(p.target_run_id)
            except Exception:
                pass

    # ── privilege info ──────────────────────────────────────────────────

    def has_edit_privilege(self) -> bool:
        return self._can_edit

    # ── data loading / rendering ────────────────────────────────────────

    def _load_calibrations(self):
        self.table.setRowCount(0)
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT dilutionid, equipmentid, element, dilution_factor,
                           dilution_factor_unc, valid_from, valid_until,
                           method, notes, createuserstamp
                    FROM ngam.ngdilutionfactor
                    ORDER BY valid_from DESC, element
                """)).fetchall()
        except Exception as e:
            QMessageBox.critical(self, "Load Error", f"Failed to load: {e}")
            return

        for r in rows:
            self._add_calibration_row(r)

    def _add_calibration_row(self, r, *, is_new: bool = False):
        idx = self.table.rowCount()
        self.table.insertRow(idx)
        dil_id = r.dilutionid if not is_new else None
        active = not r.valid_until if not is_new else True

        # Store data on the row's action widget for reading later
        act_w = QWidget()
        act_w.setProperty("dilutionid", dil_id)
        act_w.setProperty("is_new", is_new)

        # 0 – Record #
        lbl_id = QLabel(str(dil_id) if dil_id is not None else "(New)")
        lbl_id.setAlignment(Qt.AlignCenter)
        self.table.setCellWidget(idx, 0, lbl_id)

        # 1 – Equipment
        cmb_eq = QComboBox()
        cmb_eq.addItem("Lab-wide", None)
        for eid in self._eq_ids:
            cmb_eq.addItem(self._eq_map.get(eid, str(eid)), eid)
        if not is_new and r.equipmentid is not None:
            ci = cmb_eq.findData(r.equipmentid)
            if ci >= 0:
                cmb_eq.setCurrentIndex(ci)
        cmb_eq.setEnabled(self._editing)
        self.table.setCellWidget(idx, 1, cmb_eq)

        # 2 – Element
        cmb_el = QComboBox()
        cmb_el.addItems(["He", "Ne"])
        if not is_new:
            cmb_el.setCurrentText(r.element)
        cmb_el.setEnabled(self._editing)
        self.table.setCellWidget(idx, 2, cmb_el)

        # 3 – Factor
        sp_fac = QDoubleSpinBox()
        sp_fac.setRange(1.0, 10000.0); sp_fac.setDecimals(4); sp_fac.setSingleStep(0.1)
        sp_fac.setValue(float(r.dilution_factor) if not is_new else 2.4)
        sp_fac.setEnabled(self._editing)
        self.table.setCellWidget(idx, 3, sp_fac)

        # 4 – Unc
        sp_unc = QDoubleSpinBox()
        sp_unc.setRange(0.0, 1000.0); sp_unc.setDecimals(4); sp_unc.setSingleStep(0.01)
        sp_unc.setValue(float(r.dilution_factor_unc or 0.0) if not is_new else 0.0)
        sp_unc.setEnabled(self._editing)
        self.table.setCellWidget(idx, 4, sp_unc)

        # 5 – Valid From
        dt_from = QDateTimeEdit()
        dt_from.setCalendarPopup(True)
        dt_from.setDisplayFormat("yyyy-MM-dd HH:mm")
        dt_from.setEnabled(self._editing)
        if not is_new and r.valid_from:
            dt_from.setDateTime(r.valid_from if hasattr(r.valid_from, 'toPyDateTime') else r.valid_from)
        elif is_new:
            dt_from.setDateTime(QDateTime.currentDateTime())
        self.table.setCellWidget(idx, 5, dt_from)

        # 6 – Valid Until (read-only label + date display)
        vuntil_w = QWidget()
        vuntil_lay = QHBoxLayout(vuntil_w)
        vuntil_lay.setContentsMargins(0, 0, 0, 0); vuntil_lay.setSpacing(4)
        dt_until = QDateTimeEdit()
        dt_until.setCalendarPopup(True)
        dt_until.setDisplayFormat("yyyy-MM-dd HH:mm")
        dt_until.setEnabled(False)
        if active:
            dt_until.setDateTime(QDateTime.currentDateTime())
            status_lbl = QLabel("Active")
            status_lbl.setStyleSheet("font-weight: bold; color: #4CAF50;")
        else:
            dt_until.setDateTime(r.valid_until if hasattr(r.valid_until, 'toPyDateTime') else r.valid_until)
            dt_until.setEnabled(self._editing)
            status_lbl = QLabel("Inactive")
            status_lbl.setStyleSheet("font-weight: bold; color: #F44336;")
        vuntil_lay.addWidget(dt_until, 1)
        vuntil_lay.addWidget(status_lbl)
        self.table.setCellWidget(idx, 6, vuntil_w)

        # 7 – Method
        ed_method = QLineEdit((r.method or "") if not is_new else "manual")
        ed_method.setReadOnly(not self._editing)
        self.table.setCellWidget(idx, 7, ed_method)

        # 8 – Notes
        ed_notes = QLineEdit((r.notes or "") if not is_new else "")
        ed_notes.setReadOnly(not self._editing)
        self.table.setCellWidget(idx, 8, ed_notes)

        # 9 – Actions: Deactivate (active rows) or Activate (inactive rows)
        act_lay = QHBoxLayout(act_w)
        act_lay.setContentsMargins(2, 2, 2, 2); act_lay.setSpacing(4)

        if dil_id:
            if active:
                btn = QPushButton("Deactivate")
                btn.setStyleSheet(
                    "QPushButton { background: #F57C00; color: white; padding: 2px 8px;"
                    "  border-radius: 3px; border: none; font-size: 11px; }"
                    "QPushButton:hover { background: #E65100; }"
                )
                btn.clicked.connect(lambda _c, did=dil_id: self._deactivate_row(did))
            else:
                btn = QPushButton("Activate")
                btn.setStyleSheet(
                    "QPushButton { background: #2E7D32; color: white; padding: 2px 8px;"
                    "  border-radius: 3px; border: none; font-size: 11px; }"
                    "QPushButton:hover { background: #1B5E20; }"
                )
                btn.clicked.connect(lambda _c, did=dil_id: self._activate_row(did))
            btn.setVisible(self._editing)
            act_lay.addWidget(btn)

        act_lay.addStretch()
        self.table.setCellWidget(idx, 9, act_w)

    def _add_empty_row(self):
        class _FakeRow:
            pass
        r = _FakeRow()
        r.dilutionid = None; r.equipmentid = None; r.element = "He"
        r.dilution_factor = 2.4; r.dilution_factor_unc = 0.0
        r.valid_from = None; r.valid_until = None; r.method = ""; r.notes = ""
        self._add_calibration_row(r, is_new=True)

    # ── edit mode toggle ────────────────────────────────────────────────

    def _enter_edit_mode(self):
        if not self._can_edit:
            QMessageBox.warning(self, "Access Denied",
                "You need NGAM_Admin privilege to edit dilution calibrations.\n"
                "Contact your system administrator.")
            return
        # Snapshot current rows for diff / cancel
        self._original_rows = self._read_all_rows()
        self._editing = True
        self.setWindowTitle("Noble Gas Dilution Calibrations — ✎ Editing")
        self._set_editable(True)

    def _cancel_edit(self):
        reply = QMessageBox.question(self, "Cancel Edit",
            "Discard all unsaved changes and return to read-only mode?",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self._editing = False
        self.setWindowTitle("Noble Gas Dilution Calibrations — Read Only")
        self._load_calibrations()
        self._set_editable(False)

    def _set_editable(self, editable: bool):
        self.btn_edit.setVisible(not editable)
        self.btn_add.setVisible(editable)
        self.btn_save_all.setVisible(editable)
        self.btn_cancel_edit.setVisible(editable)

        for r in range(self.table.rowCount()):
            for c in (1, 2, 3, 4, 5):
                w = self.table.cellWidget(r, c)
                if w:
                    w.setEnabled(editable)
            # col 6 – valid_until: only enable date edit when Inactive and editable
            vw = self.table.cellWidget(r, 6)
            if vw:
                dt = vw.findChild(QDateTimeEdit)
                lbl = vw.findChild(QLabel)
                if dt and lbl:
                    dt.setEnabled(editable and lbl.text() == "Inactive")
            # col 7, 8 – readOnly
            for c in (7, 8):
                w = self.table.cellWidget(r, c)
                if isinstance(w, QLineEdit):
                    w.setReadOnly(not editable)
            # col 9 – actions
            aw = self.table.cellWidget(r, 9)
            if aw:
                for ch in aw.children():
                    if isinstance(ch, QPushButton) and ch.text() != "":
                        ch.setVisible(editable)

    # ── read / save / deactivate ────────────────────────────────────────

    def _read_all_rows(self) -> list:
        """Read all row data from the table. Returns list of dicts."""
        rows = []
        for r in range(self.table.rowCount()):
            data = self._read_row(r)
            if data:
                rows.append(data)
        return rows

    def _read_row(self, row: int) -> Optional[Dict]:
        try:
            cmb_eq = self.table.cellWidget(row, 1)
            cmb_el = self.table.cellWidget(row, 2)
            sp_fac = self.table.cellWidget(row, 3)
            sp_unc = self.table.cellWidget(row, 4)
            dt_from = self.table.cellWidget(row, 5)
            vuntil_w = self.table.cellWidget(row, 6)
            ed_meth = self.table.cellWidget(row, 7)
            ed_note = self.table.cellWidget(row, 8)
            act_w = self.table.cellWidget(row, 9)
            if not all([cmb_eq, cmb_el, sp_fac, sp_unc, dt_from, vuntil_w, ed_meth, ed_note]):
                return None

            dt_until = vuntil_w.findChild(QDateTimeEdit)
            lbl_status = vuntil_w.findChild(QLabel)
            is_active = lbl_status.text() == "Active" if lbl_status else True
            vfrom = dt_from.dateTime().toPyDateTime()
            vuntil = None if is_active else dt_until.dateTime().toPyDateTime().isoformat() if dt_until else None

            dil_id = act_w.property("dilutionid") if act_w else None
            is_new = act_w.property("is_new") if act_w else False

            return {
                "dilutionid": dil_id,
                "is_new": is_new,
                "equipment_id": cmb_eq.currentData(),
                "element": cmb_el.currentText(),
                "dilution_factor": sp_fac.value(),
                "dilution_factor_unc": sp_unc.value(),
                "valid_from": vfrom.isoformat(),
                "valid_until": vuntil,
                "method": ed_meth.text().strip(),
                "notes": ed_note.text().strip(),
            }
        except Exception:
            return None

    def _save_all(self):
        if not self._editing:
            return
        all_rows = self._read_all_rows()
        if not all_rows:
            return

        # Validate: one Active per equipment + element
        active_keys: set = set()
        for data in all_rows:
            if data["valid_until"] is None:
                key = (data["equipment_id"], data["element"])
                if key in active_keys:
                    QMessageBox.critical(self, "Validation Error",
                        f"Multiple active calibrations for {data['element']} on the same instrument.\n"
                        "Only one can be Active at a time. Deactivate the other one before saving.")
                    return
                active_keys.add(key)

        # Validate: no duplicate Active with existing DB records
        try:
            with db_manager.get_connection() as conn:
                for data in all_rows:
                    if data["valid_until"] is None:
                        existing = conn.execute(text("""
                            SELECT dilutionid FROM ngam.ngdilutionfactor
                            WHERE (equipmentid = :eq OR (equipmentid IS NULL AND :eq IS NULL))
                              AND element = :elem AND valid_until IS NULL
                              AND (:did IS NULL OR dilutionid <> :did)
                            LIMIT 1
                        """), {"eq": data["equipment_id"], "elem": data["element"],
                                "did": data["dilutionid"]}).fetchone()
                        if existing:
                            QMessageBox.critical(self, "Validation Error",
                                f"An active calibration for {data['element']} on this instrument "
                                f"already exists (record {existing[0]}).\n"
                                "Deactivate it first before activating a new one.")
                            return
        except Exception as e:
            QMessageBox.critical(self, "DB Error", f"Could not verify active calibrations: {e}")
            return

        # Count changes
        changed = sum(1 for d in all_rows if d["is_new"]) + len(
            [d for d in all_rows if not d["is_new"] and d["dilutionid"]])
        if changed == 0:
            QMessageBox.information(self, "No Changes", "Nothing to save.")
            return

        reply = QMessageBox.question(self, "Confirm Save",
            f"Save {len(all_rows)} calibration record(s)?\n"
            "Changes will be written to the database immediately.",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        try:
            from shared_utils import get_current_user_id, normalize_login_name
            uid = get_current_user_id()
            user = normalize_login_name(uid) if uid else "unknown"
        except Exception:
            user = "unknown"

        errors = []
        with db_manager.get_connection() as conn:
            for data in all_rows:
                try:
                    if data["dilutionid"] and not data["is_new"]:
                        conn.execute(text("""
                            UPDATE ngam.ngdilutionfactor
                            SET equipmentid = :eq, valid_from = :vfrom, valid_until = :vuntil,
                                element = :elem, dilution_factor = :factor, dilution_factor_unc = :unc,
                                method = :method, notes = :notes
                            WHERE dilutionid = :did
                        """), {
                            "eq": data["equipment_id"], "vfrom": data["valid_from"],
                            "vuntil": data["valid_until"], "elem": data["element"],
                            "factor": data["dilution_factor"], "unc": data["dilution_factor_unc"],
                            "method": data["method"], "notes": data["notes"], "did": data["dilutionid"],
                        })
                    else:
                        conn.execute(text("""
                            INSERT INTO ngam.ngdilutionfactor
                            (equipmentid, valid_from, valid_until, element, dilution_factor,
                             dilution_factor_unc, method, notes, createuserstamp)
                            VALUES (:eq, :vfrom, :vuntil, :el, :fac, :unc, :meth, :note, :user)
                        """), {
                            "eq": data["equipment_id"], "vfrom": data["valid_from"],
                            "vuntil": data["valid_until"], "el": data["element"],
                            "fac": data["dilution_factor"], "unc": data["dilution_factor_unc"],
                            "meth": data["method"], "note": data["notes"], "user": user,
                        })
                except Exception as e:
                    errors.append(f"Row {data['element']}: {e}")
            conn.commit()

        if errors:
            QMessageBox.critical(self, "Partial Failure",
                "Some records could not be saved:\n" + "\n".join(errors))
        else:
            QMessageBox.information(self, "Saved",
                f"{len(all_rows)} record(s) saved successfully.")

        self._editing = False
        self.setWindowTitle("Noble Gas Dilution Calibrations — Read Only")
        self._load_calibrations()
        self._set_editable(False)

    def _deactivate_row(self, dil_id: int):
        elem = "unknown"
        equip = "unknown"
        for r in range(self.table.rowCount()):
            act_w = self.table.cellWidget(r, 9)
            if act_w and act_w.property("dilutionid") == dil_id:
                cmb_eq = self.table.cellWidget(r, 1)
                cmb_el = self.table.cellWidget(r, 2)
                elem = cmb_el.currentText() if cmb_el else "unknown"
                equip = cmb_eq.currentText() if cmb_eq else "unknown"
                break

        reply = QMessageBox.question(
            self, "Confirm Deactivate",
            f"Set end date on calibration record {dil_id} ({elem} on {equip})?\n"
            "It will no longer be active after today.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        from datetime import datetime
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text(
                    "UPDATE ngam.ngdilutionfactor SET valid_until = :now WHERE dilutionid = :did"
                ), {"now": datetime.now(), "did": dil_id})
                conn.commit()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to deactivate: {e}")
            return
        self._load_calibrations()

    def _activate_row(self, dil_id: int):
        """Reactivate an inactive calibration by clearing its valid_until."""
        elem = "unknown"
        equip = "unknown"
        for r in range(self.table.rowCount()):
            act_w = self.table.cellWidget(r, 9)
            if act_w and act_w.property("dilutionid") == dil_id:
                cmb_eq = self.table.cellWidget(r, 1)
                cmb_el = self.table.cellWidget(r, 2)
                elem = cmb_el.currentText() if cmb_el else "unknown"
                equip = cmb_eq.currentText() if cmb_eq else "unknown"
                break

        reply = QMessageBox.question(
            self, "Confirm Activate",
            f"Reactivate calibration record {dil_id} ({elem} on {equip})?\n"
            "It will become active again with no end date.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text(
                    "UPDATE ngam.ngdilutionfactor SET valid_until = NULL WHERE dilutionid = :did"
                ), {"did": dil_id})
                conn.commit()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to activate: {e}")
            return
        self._load_calibrations()
class ImportFromProtocolWidget(QWidget):
    """
    NGAM widget: browse → parse → process (with interactive outlier override)
    → import to DB. Refactored to MVVM architecture.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        target_run_id: Optional[int] = None,
    ) -> None:
        super().__init__(parent)
        self.model = NGAMModel(target_run_id=target_run_id)
        self.viewmodel = NGAMViewModel(self.model, parent=self)
        self._progress_dialog: Optional[QProgressDialog] = None
        self._selected_filepath: str = ""
        self._setup_ui()
        self._connect_viewmodel_signals()

        if target_run_id:
            try:
                from ngam_processor.database import ngam_repository
                self._db_dilution_factors = ngam_repository.build_dilution_info(target_run_id)
            except Exception as e:
                self._db_dilution_factors = {}
                log.warning(f"Failed to populate dilution factors from database: {e}")
        else:
            self._db_dilution_factors = {}

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 6)
        root.setSpacing(6)

        # ── Toolbar (two rows) ───────────────────────────────────────
        toolbar_wrapper = QVBoxLayout()
        toolbar_wrapper.setSpacing(4)

        # Row 1: file + action buttons
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        self._btn_browse = QPushButton("Browse .protocol…")
        self._btn_browse.setStyleSheet(_BTN_GLASS)
        _apply_glass_glow(self._btn_browse)
        self._btn_browse.clicked.connect(self._browse)
        row1.addWidget(self._btn_browse)

        self._btn_clear = QPushButton("Clear")
        self._btn_clear.setStyleSheet(_BTN_GLASS)
        _apply_glass_glow(self._btn_clear)
        self._btn_clear.setToolTip("Clear the parsed sequence and reset model/views")
        self._btn_clear.clicked.connect(self._on_clear_clicked)
        self._btn_clear.setVisible(False)
        row1.addWidget(self._btn_clear)

        row1.addStretch(1)

        self._btn_process = QPushButton("Process Data")
        self._btn_process.setStyleSheet(_BTN_GLASS)
        _apply_glass_glow(self._btn_process)
        self._btn_process.setEnabled(False)
        self._btn_process.clicked.connect(self._do_process)
        row1.addWidget(self._btn_process)

        self._btn_import = QPushButton("Import to DB")
        self._btn_import.setStyleSheet(_BTN_GLASS)
        _apply_glass_glow(self._btn_import)
        self._btn_import.setEnabled(False)
        self._btn_import.clicked.connect(self._do_import)
        row1.addWidget(self._btn_import)

        row1.addWidget(make_help_button(self, "ngam_ng_import"))

        btn_close = QPushButton("Close")
        btn_close.setStyleSheet(_BTN_GLASS)
        _apply_glass_glow(btn_close)
        btn_close.setToolTip("Close this dialog")
        btn_close.clicked.connect(self._on_close_clicked)
        row1.addWidget(btn_close)

        toolbar_wrapper.addLayout(row1)

        # Row 2: processing parameter controls
        row2 = QHBoxLayout()
        row2.setSpacing(6)

        row2.addWidget(QLabel("Outlier:"))
        self._combo_outlier = QComboBox()
        self._combo_outlier.addItem("MAD", "mad")
        self._combo_outlier.addItem("N-sigma", "nsigma")
        self._combo_outlier.addItem("Huber", "huber")
        self._combo_outlier.addItem("None", "none")
        self._combo_outlier.setFixedWidth(90)
        self._combo_outlier.currentIndexChanged.connect(self._on_outlier_method_changed)
        row2.addWidget(self._combo_outlier)

        self._spin_nsigma = QDoubleSpinBox()
        self._spin_nsigma.setRange(1.0, 5.0)
        self._spin_nsigma.setSingleStep(0.5)
        self._spin_nsigma.setValue(2.5)
        self._spin_nsigma.setDecimals(1)
        self._spin_nsigma.setFixedWidth(58)
        row2.addWidget(self._spin_nsigma)

        self._btn_reset_outliers = QPushButton("↺")
        self._btn_reset_outliers.setFixedSize(28, 28)
        self._btn_reset_outliers.setStyleSheet(
            "QPushButton { background: #BF360C; color: white; font-weight: bold;"
            "  border: none; border-radius: 4px; font-size: 14px; }"
            "QPushButton:hover { background: #8D2000; }"
            "QPushButton:disabled { background: #B0BEC5; color: #78909C; }"
        )
        self._btn_reset_outliers.setToolTip("Reset Outliers — discard all manual overrides and re-run auto-detection")
        self._btn_reset_outliers.setEnabled(False)
        row2.addWidget(self._btn_reset_outliers)

        row2.addSpacing(8)
        row2.addWidget(QLabel("Blank:"))
        self._combo_blank = QComboBox()
        self._combo_blank.addItem("Auto (AICc)",   "auto")
        self._combo_blank.addItem("Mean (const.)", "mean")
        self._combo_blank.addItem("Akima (smooth)", "akima")
        self._combo_blank.addItem("Linear fit",    "linear")
        self._combo_blank.addItem("Quadratic fit", "quadratic")
        self._combo_blank.addItem("Cubic fit",     "cubic")
        self._combo_blank.setFixedWidth(120)
        self._combo_blank.setToolTip(
            "Blank interpolation:\n"
            "  Auto (AICc) — AICc selects the best polynomial per isotope\n"
            "    (mean / linear / poly2 / poly3); recommended default\n"
            "  Mean — constant average of all blanks\n"
            "  Akima — smooth spline through blanks vs time; no oscillation\n"
            "    between knots (requires ≥3 blanks, else falls back to linear)\n"
            "  Linear/Quadratic/Cubic — fixed polynomial fit blank vs time"
        )
        row2.addWidget(self._combo_blank)

        row2.addSpacing(6)
        row2.addWidget(QLabel("Drift:"))
        self._combo_drift = QComboBox()
        self._combo_drift.addItem("Auto (AICc)",       "auto")
        self._combo_drift.addItem("None (mean sens.)", "none")
        self._combo_drift.addItem("Akima (smooth)",    "akima")
        self._combo_drift.addItem("Linear",            "linear")
        self._combo_drift.addItem("Quadratic",         "quadratic")
        self._combo_drift.addItem("Cubic",             "cubic")
        self._combo_drift.addItem("Exponential",       "exponential")
        self._combo_drift.setFixedWidth(140)
        self._combo_drift.setToolTip(
            "Sensitivity drift correction:\n"
            "  Auto (AICc) — AICc selects the best polynomial per isotope\n"
            "    (mean / linear / poly2 / poly3); recommended when drift uncertain\n"
            "  None — use mean sensitivity (no drift correction)\n"
            "  Akima — smooth spline through S_i vs time; locally fitted,\n"
            "    no oscillation from outlier standards; clamped at endpoints\n"
            "    (requires ≥3 standards, else falls back to linear)\n"
            "  Linear/Quadratic/Cubic — global polynomial fit\n"
            "  Exponential — y = a·exp(b·t) fit (falls back to linear if\n"
            "    any sensitivity ≤ 0)"
        )
        row2.addWidget(self._combo_drift)

        row2.addSpacing(6)
        row2.addWidget(QLabel("Linearity:"))
        self._combo_linearity = QComboBox()
        self._combo_linearity.addItem("Auto (AICc)", "auto")
        self._combo_linearity.addItem("None",        "none")
        self._combo_linearity.addItem("Linear",      "linear")
        self._combo_linearity.addItem("Quadratic",   "quadratic")
        self._combo_linearity.setFixedWidth(110)
        self._combo_linearity.setToolTip(
            "Detector linearity correction:\n"
            "  Auto (AICc) — AICc selects linear or quadratic correction\n"
            "  None — no correction (use mean sensitivity)\n"
            "  Linear/Quadratic — fixed polynomial fit S vs signal level;\n"
            "    samples calibrated with S(bc_sample)"
        )
        row2.addWidget(self._combo_linearity)

        row2.addSpacing(8)
        row2.addWidget(QLabel("τ SEM:"))
        self._spin_dead_time = QDoubleSpinBox()
        self._spin_dead_time.setRange(0.0, 1.0)
        self._spin_dead_time.setSingleStep(0.001)
        self._spin_dead_time.setValue(0.0)
        self._spin_dead_time.setDecimals(4)
        self._spin_dead_time.setFixedWidth(72)
        self._spin_dead_time.setToolTip(
            "SEM dead-time correction (3He multiplier only).\n"
            "Formula: n_true = n / (1 − n · τ)\n"
            "τ must be in the same unit as the Multiplier signal column.\n"
            "  • If signals are in CPS: enter τ in seconds (e.g. 20e-9 for 20 ns).\n"
            "  • If signals are in a scaled unit (NobleControl typical range\n"
            "    0.1–0.2 for large standards): determine τ empirically from\n"
            "    a linearity curve, or consult the instrument manual.\n"
            "Default 0 = disabled."
        )
        row2.addWidget(self._spin_dead_time)

        row2.addSpacing(8)
        row2.addWidget(QLabel("Gauge σ:"))
        self._spin_gauge_sigma = QDoubleSpinBox()
        self._spin_gauge_sigma.setRange(1.0, 5.0)
        self._spin_gauge_sigma.setSingleStep(0.5)
        self._spin_gauge_sigma.setValue(3.0)
        self._spin_gauge_sigma.setDecimals(1)
        self._spin_gauge_sigma.setFixedWidth(52)
        self._spin_gauge_sigma.setToolTip(
            "σ-clipping threshold for SRG / pressure gauge outlier removal.\n"
            "Points whose residuals from a linear detrend exceed this multiple\n"
            "of the standard deviation are excluded before computing the\n"
            "time-weighted mean per inlet.  Default 3.0."
        )
        row2.addWidget(self._spin_gauge_sigma)

        self._chk_gauge_qc = QCheckBox("Gauge QC")
        self._chk_gauge_qc.setChecked(True)
        self._chk_gauge_qc.setToolTip(
            "When checked, inlets with anomalously high SRG pressure\n"
            "(> 3× run median) are flagged in the QC tab and Gauge tab.\n"
            "A high reading during a sample inlet may indicate a leak\n"
            "or outgassing event that could have contaminated that inlet."
        )
        row2.addWidget(self._chk_gauge_qc)

        row2.addStretch(1)
        toolbar_wrapper.addLayout(row2)

        # Row 3: dilution & IDMS parameter controls
        row3 = QHBoxLayout()
        row3.setSpacing(6)

        self._chk_dilution = QCheckBox("Dilution")
        self._chk_dilution.setToolTip(
            "Multiply ccSTP by per-inlet dilution factors (≥1.0).\n"
            "Click the Df… button next to this checkbox, set each inlet's\n"
            "factor, then enable this checkbox and click Process Data."
        )
        row3.addWidget(self._chk_dilution)

        self._btn_dilution = QPushButton("Df…")
        self._btn_dilution.setFixedWidth(36)
        self._btn_dilution.setStyleSheet(
            "QPushButton { background: #546E7A; color: white; font-weight: bold;"
            "  border: none; padding: 2px 6px; border-radius: 3px; font-size: 11px; }"
            "QPushButton:hover { background: #37474F; }"
        )
        self._btn_dilution.setToolTip("Edit per-element Helium / Neon dilution factor calibrations")
        self._btn_dilution.clicked.connect(self._on_dilution_clicked)
        row3.addWidget(self._btn_dilution)

        row3.addSpacing(8)
        self._chk_idms = QCheckBox("IDMS")
        self._chk_idms.setToolTip(
            "Isotope Dilution Mass Spectrometry — override sensitivity-based\n"
            "ccSTP with direct isotope-ratio calculation from a spiked standard."
        )
        self._chk_idms.toggled.connect(self._on_idms_toggled)
        row3.addWidget(self._chk_idms)

        row3.addSpacing(8)
        self._combo_bg_proxy = QComboBox()
        self._combo_bg_proxy.addItem("BG Proxy: Always", "always")
        self._combo_bg_proxy.addItem("BG Proxy: Auto", "auto")
        self._combo_bg_proxy.addItem("BG Proxy: Off", "off")
        self._combo_bg_proxy.setToolTip(
            "Configure background proxy (using simultaneously measured 3He background for 4He):\n"
            "- Always: Force use proxy background\n"
            "- Auto: Use proxy only if original background is unreliable\n"
            "- Off: Use original background measurement"
        )
        self._combo_bg_proxy.currentIndexChanged.connect(self._on_bg_proxy_changed)
        row3.addWidget(self._combo_bg_proxy)

        self._lbl_proxy_factor = QLabel("Factor α:")
        row3.addWidget(self._lbl_proxy_factor)
        self._spin_proxy_factor = QDoubleSpinBox()
        self._spin_proxy_factor.setRange(0.0001, 1000000.0)
        self._spin_proxy_factor.setSingleStep(10.0)
        self._spin_proxy_factor.setValue(100.0)
        self._spin_proxy_factor.setDecimals(4)
        self._spin_proxy_factor.setFixedWidth(80)
        self._spin_proxy_factor.setToolTip(
            "Baseline conversion multiplier (alpha) to bridge magnitude difference\n"
            "between Faraday cup noise floor and electron multiplier dark counts."
        )
        self._spin_proxy_factor.setEnabled(True)
        row3.addWidget(self._spin_proxy_factor)

        row3.addStretch(1)
        toolbar_wrapper.addLayout(row3)

        root.addLayout(toolbar_wrapper)

        # ── Status bar (under tree list in results widget) ───────────
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(
            "QLabel { color: #546E7A; font-size: 12px; font-weight: bold;"
            "  padding: 4px 6px; border-top: 1px solid #CFD8DC; background: #FAFAFA; }"
        )

        # ── Shared results widget ─────────────────────────────────────
        self._results_widget = QWidget()
        try:
            self._results_widget = NGAMResultsWidget(self)
        except Exception as e:
            log.warning(f"Failed to load NGAMResultsWidget: {e}")
        root.addWidget(self._results_widget, 1)

        if hasattr(self, "_results_widget") and hasattr(self._results_widget, "add_inlet_list_action"):
            self._results_widget.add_inlet_list_action(self._status_lbl)

        # Wire reset button to results widget
        if hasattr(self._results_widget, "reset_outliers"):
            self._btn_reset_outliers.clicked.connect(self._results_widget.reset_outliers)
        if hasattr(self._results_widget, "can_reset_changed"):
            self._results_widget.can_reset_changed.connect(self._btn_reset_outliers.setEnabled)

    def _connect_viewmodel_signals(self) -> None:
        self.viewmodel.statusChanged.connect(self._status_lbl.setText)
        
        self.viewmodel.parsingFinished.connect(self._on_parsing_finished)
        self.viewmodel.parsingFailed.connect(self._on_parsing_failed)
        
        self.viewmodel.processingFinished.connect(self._on_processing_finished)
        self.viewmodel.processingFailed.connect(self._on_processing_failed)
        
        self.viewmodel.progressUpdated.connect(self._on_progress_updated)
        self.viewmodel.importFinished.connect(self._on_import_finished)
        self.viewmodel.importFailed.connect(self._on_import_failed)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        return

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def _on_clear_clicked(self) -> None:
        """Clear button: resets sequence/model and UI without closing."""
        self.model.reset()
        if hasattr(self, "_results_widget") and hasattr(self._results_widget, "clear"):
            self._results_widget.clear()
        self.reset_ui_state()

    def _on_close_clicked(self) -> None:
        """Close button: works whether embedded in AppShell or a QDialog."""
        self.model.reset()
        if hasattr(self, "_results_widget") and hasattr(self._results_widget, "clear"):
            self._results_widget.clear()
        self.reset_ui_state()
        w = self.window()
        if hasattr(w, "reject"):        # standalone QDialog
            w.reject()
        elif hasattr(w, "show_home"):   # embedded in AppShell
            w.show_home()

    def reset_ui_state(self) -> None:
        """Restore the Browse button, action buttons, and status label to their initial state."""
        self._selected_filepath = ""
        self._btn_process.setEnabled(False)
        self._btn_import.setEnabled(False)
        self._btn_clear.setVisible(False)
        
        # Reset the Browse button text, tooltip, and style
        self._btn_browse.setText("Browse .protocol…")
        self._btn_browse.setToolTip("")
        self._btn_browse.setStyleSheet(_BTN_GLASS)
        
        # Reconnect clicked to _browse
        try:
            self._btn_browse.clicked.disconnect()
        except Exception:
            pass
        self._btn_browse.clicked.connect(self._browse)
        
        # Reset status label
        self._status_lbl.setText("Ready")
        self._status_lbl.setStyleSheet(
            "QLabel { color: #546E7A; font-size: 12px; font-weight: bold;"
            "  padding: 4px 6px; border-top: 1px solid #CFD8DC; background: #FAFAFA; }"
        )

    # Browse / Parse / Process
    # ------------------------------------------------------------------

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Protocol File", "",
            "Protocol Files (*.protocol);;All Files (*)",
        )
        if not path:
            return
        self._selected_filepath = path
        self._btn_import.setEnabled(False)
        self._btn_process.setEnabled(False)
        self.model.sequence = None
        self.model.supplemental_inlets = []
        self._parse()

    def _parse(self) -> None:
        path = self._selected_filepath
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, "Parse Error", "Please select a valid .protocol file.")
            return
        self.viewmodel.parse_file(path)

    def _on_parsing_finished(self) -> None:
        seq = self.model.sequence
        if not seq:
            return
        self._btn_import.setEnabled(False)
        self._btn_process.setEnabled(True)
        self._btn_clear.setVisible(True)
        # Morph the Browse button into Supplemental Runs
        self._btn_browse.setText("Supplemental Runs…")
        self._btn_browse.setToolTip("Add blanks/standards from other runs to improve calibration")
        self._btn_browse.setStyleSheet(
            "QPushButton { background:#6A1B9A; color:white; font-weight:bold;"
            "  border:none; padding:5px 12px; border-radius:4px; }"
            "QPushButton:hover { background:#4A148C; }"
        )
        try:
            self._btn_browse.clicked.disconnect()
        except Exception:
            pass
        self._btn_browse.clicked.connect(self._open_supplemental_dialog)
        
        if hasattr(self._results_widget, "preview_sequence"):
            self._results_widget.preview_sequence(seq)

        self._apply_suggested_config(seq)
        self._flash_status(
            f"{len(seq.inlets)} inlet(s) parsed — "
            f"Start: {lv_to_local_str(seq.lv_time_start)}. "
            f"Add supplemental runs if needed, then click Process Data."
        )

    def _on_parsing_failed(self, error_msg: str) -> None:
        QMessageBox.critical(self, "Parse Error", f"Failed to parse file:\n{error_msg}")
        self._flash_status("Parse failed.", is_success=False)

    def _open_supplemental_dialog(self) -> None:
        dlg = SupplementalRunsDialog(self, self.model.sequence, self.model.supplemental_inlets or [])
        if dlg.exec_() != SupplementalRunsDialog.Accepted:
            return
        self.model.supplemental_inlets = dlg.get_selected_inlets()
        n = len(self.model.supplemental_inlets)
        # Rebuild preview with supplemental inlets merged in
        preview_seq = merge_sequences(self.model.sequence, self.model.supplemental_inlets) if self.model.supplemental_inlets else self.model.sequence
        if hasattr(self._results_widget, "preview_sequence"):
            self._results_widget.preview_sequence(preview_seq)
        self._status_lbl.setText(
            f"{n} supplemental inlet(s) added — Click Process Data to apply."
        )

    def _do_process(self) -> None:
        # Sync view input to model config
        self.model.config.outlier_method = self._combo_outlier.currentData()
        self.model.config.nsigma_threshold = self._spin_nsigma.value()
        self.model.config.blank_interpolation = self._combo_blank.currentData()
        self.model.config.drift_correction = self._combo_drift.currentData()
        self.model.config.linearity_correction = self._combo_linearity.currentData()
        self.model.config.dead_time_tau = self._spin_dead_time.value()
        self.model.config.gauge_sigma = self._spin_gauge_sigma.value()
        self.model.config.gauge_qc_flags = self._chk_gauge_qc.isChecked()
        self.model.config.dilution_enabled = self._chk_dilution.isChecked()
        self.model.config.he_dilution_factor = self._db_dilution_factors.get("He", 2.4)
        self.model.config.ne_dilution_factor = self._db_dilution_factors.get("Ne", 2.4)
        self.model.config.idms_enabled = self._chk_idms.isChecked()

        # Sync BG Proxy config
        bg_mode = self._combo_bg_proxy.currentData()
        self.model.config.bg_proxy_mode = bg_mode
        if bg_mode in ("always", "auto"):
            self.model.config.bg_proxy_map = {"4He": "3He"}
            self.model.config.bg_proxy_factors = {"4He": self._spin_proxy_factor.value()}
        else:
            self.model.config.bg_proxy_map = {}
            self.model.config.bg_proxy_factors = {}

        if hasattr(self, "_results_widget") and hasattr(self._results_widget, "_fit_model_overrides"):
            self.model.fit_model_overrides = self._results_widget._fit_model_overrides

        # Collect IDMS config from the dialog state
        if self._chk_idms.isChecked() and hasattr(self, "_idms_state"):
            idms_cfg = dict(self._idms_state)
            if self.model.sequence:
                for target_iso, entry in idms_cfg.items():
                    spike_labid = entry.get("spike_standard", "")
                    sample_inlets: Dict[int, str] = {}
                    for prep in self.model.sequence.inlets:
                        is_blank = prep.is_blank or "blank" in prep.inlet_string.lower()
                        if not is_blank and not prep.is_reference:
                            sample_inlets[prep.seq_num] = spike_labid
                    entry["spike_per_inlet"] = sample_inlets
            self.model.idms_config = idms_cfg
        else:
            self.model.idms_config = {}

        self.viewmodel.process_data()

    def _on_processing_finished(self) -> None:
        seq = self.model.sequence
        result = self.model.result
        config = self.model.config
        if not seq or not result:
            return

        preview_seq = merge_sequences(seq, self.model.supplemental_inlets) if self.model.supplemental_inlets else seq

        self._btn_import.setEnabled(True)

        if hasattr(self._results_widget, "set_data"):
            self._results_widget.set_data(preview_seq, result, config,
                                          repro_references=self.model.repro_references,
                                          multi_run_linearity=self.model.multi_run_linearity,
                                          aliquot_volumes=self.model.aliquot_volumes,
                                          fit_model_overrides=self.model.fit_model_overrides,
                                          excluded_standards=self.model.excluded_standards,
                                          excluded_blanks=self.model.excluded_blanks,
                                          force_included_standards=self.model.force_included_standards,
                                          force_included_blanks=self.model.force_included_blanks,
                                          blank_fit_overrides=self.model.blank_fit_overrides,
                                          drift_fit_overrides=self.model.drift_fit_overrides,
                                          linearity_fit_overrides=self.model.linearity_fit_overrides,
                                          unc_trace=self.model.unc_trace)
            try:
                self._results_widget.result_changed.disconnect()
                self._results_widget.outliers_reset.disconnect()
            except Exception:
                pass
            self._results_widget.result_changed.connect(self._on_result_changed)
            self._results_widget.outliers_reset.connect(self._on_outliers_reset)

        total_outliers = sum(
            bf.n_outliers
            for ir in result.inlets
            for dev_fits in ir.block_fits.values()
            for bf in dev_fits.values()
        )
        msg = (
            f"Processed — {result.n_blanks} blank(s), {result.n_standards} standard(s), "
            f"{result.n_samples} sample(s). {total_outliers} auto-outlier(s) flagged."
        )
        if self.model.fit_model_overrides:
            msg += " Reprocessed with custom fit model overrides."
        else:
            msg += " Toggle checkboxes in the Signals tab to override."

        # Surface calibration/signal-collapse warnings from the pipeline
        # (e.g. missing certified values, cycles dropped instantaneously)
        # instead of silently discarding them -- these indicate the result
        # may need review, not just informational status.
        warnings = list(getattr(result, "calibration_warnings", []) or []) + \
            list(getattr(result, "signal_collapse_warnings", []) or [])
        if warnings:
            msg += "  ⚠ " + "; ".join(warnings)
            self._flash_status(msg, is_success=False)
        else:
            self._flash_status(msg)

    def _on_processing_failed(self, error_msg: str) -> None:
        QMessageBox.critical(self, "Processing Error", f"Failed:\n{error_msg}")
        self._flash_status("Processing failed.", is_success=False)

    def _on_result_changed(self, new_result) -> None:
        self.model.result = new_result
        # Sync interactive right-click/combo edits made in the results widget
        # back into the model, so a later explicit "Process Data" (which
        # rebuilds from self.model.*) doesn't silently discard them.
        rw = self._results_widget
        self.model.excluded_standards = dict(getattr(rw, "_excluded_standards", {}) or {})
        self.model.excluded_blanks = dict(getattr(rw, "_excluded_blanks", {}) or {})
        self.model.force_included_standards = dict(getattr(rw, "_force_included_standards", {}) or {})
        self.model.force_included_blanks = dict(getattr(rw, "_force_included_blanks", {}) or {})
        self.model.blank_fit_overrides = dict(getattr(rw, "_blank_fit_overrides", {}) or {})
        self.model.drift_fit_overrides = dict(getattr(rw, "_drift_fit_overrides", {}) or {})
        self.model.linearity_fit_overrides = dict(getattr(rw, "_linearity_fit_overrides", {}) or {})
        total = sum(
            bf.n_outliers
            for ir in new_result.inlets
            for df in ir.block_fits.values()
            for bf in df.values()
        )
        self._flash_status(
            f"Re-processed after manual override — {total} outlier(s) effective."
        )

    def _on_outliers_reset(self, new_result) -> None:
        self.model.result = new_result
        total = sum(
            bf.n_outliers
            for ir in new_result.inlets
            for df in ir.block_fits.values()
            for bf in df.values()
        )
        self._flash_status(
            f"Outliers reset — {total} auto-detected across all inlets."
        )

    def _on_outlier_method_changed(self) -> None:
        self._spin_nsigma.setEnabled(
            self._combo_outlier.currentData() != "none"
        )

    # ------------------------------------------------------------------
    # Dilution factor dialog
    # ------------------------------------------------------------------

    def _on_dilution_clicked(self) -> None:
        """Open the unified dilution factor calibrations dialog."""
        dlg = DilutionCalibrationsDialog(self, target_run_id=self.model.target_run_id)
        dlg.exec_()
        # Refresh cached factors from DB after dialog closes
        if self.model.target_run_id:
            try:
                from ngam_processor.database import ngam_repository
                self._db_dilution_factors = ngam_repository.build_dilution_info(self.model.target_run_id)
            except Exception as e:
                log.warning(f"Failed to refresh dilution factors from database: {e}")

    # History button removed as redundant

    # ------------------------------------------------------------------
    # IDMS configuration dialog
    # ------------------------------------------------------------------

    def _collect_isotopes(self) -> list:
        """Return sorted unique isotope names across all inlets in the sequence."""
        all_isotopes: set = set()
        if not self.model.sequence:
            return []
        for prep in self.model.sequence.inlets:
            for ms in prep.ms_data:
                nucs = ms.nuclides
                if nucs:
                    for nuc in nucs:
                        all_isotopes.add(nuc)
                else:
                    def_nucs = _DEVICE_DEFAULT_NUCLIDES.get(ms.device, [])
                    for nuc in def_nucs:
                        all_isotopes.add(nuc)
        return sorted(all_isotopes)

    def _on_bg_proxy_changed(self, index: int) -> None:
        mode = self._combo_bg_proxy.currentData()
        self._spin_proxy_factor.setEnabled(mode in ("always", "auto"))

    def _on_idms_toggled(self, checked: bool) -> None:
        """Open the IDMS configuration dialog when the checkbox is ticked."""
        if not checked:
            self.model.idms_config = {}
            return
        if not self.model.sequence or not self.model.sequence.inlets:
            QMessageBox.information(self, "IDMS", "Please parse a protocol file first.")
            self._chk_idms.blockSignals(True)
            self._chk_idms.setChecked(False)
            self._chk_idms.blockSignals(False)
            return

        dlg = _IdmsConfigDialog(
            self,
            isotopes=self._collect_isotopes(),
            previous_state=getattr(self, "_idms_state", None),
        )
        if dlg.exec_() != _IdmsConfigDialog.Accepted:
            self._chk_idms.blockSignals(True)
            self._chk_idms.setChecked(False)
            self._chk_idms.blockSignals(False)
            return
        self._idms_state = dlg.get_config()

    def _apply_suggested_config(self, seq) -> None:
        """Auto-select suggested configuration based on blanks and standards counts."""
        def _itype(p):
            if p.is_blank or "blank" in p.inlet_string.lower():
                return "blank"
            if p.is_reference and p.reference_amount > 0:
                return "standard"
            return "sample"

        inlets = seq.inlets
        n_blanks = sum(1 for p in inlets if _itype(p) == "blank")
        stds = [p for p in inlets if _itype(p) == "standard"]
        n_stds = len(stds)

        # Blank interpolation — Auto (AICc) when enough blanks for comparison
        blank_val = "auto" if n_blanks >= 2 else "mean"

        # Drift correction — Auto (AICc) when enough standards for comparison
        drift_val = "auto" if n_stds >= 2 else "none"

        # Linearity: meaningful only when standards span different amounts
        amounts = [p.reference_amount for p in stds if p.reference_amount > 0]
        if len(amounts) >= 2:
            amt_mean  = sum(amounts) / len(amounts)
            amt_range = max(amounts) - min(amounts)
            lin_val = "auto" if amt_mean > 0 and amt_range / amt_mean > 0.10 else "none"
        else:
            lin_val = "none"

        def _set_combo(combo, val):
            idx = combo.findData(val)
            if idx >= 0:
                combo.setCurrentIndex(idx)

        _set_combo(self._combo_blank,    blank_val)
        _set_combo(self._combo_drift,    drift_val)
        _set_combo(self._combo_linearity, lin_val)

    # Database import
    # ------------------------------------------------------------------

    def _do_import(self) -> None:
        if self.model.sequence is None:
            QMessageBox.warning(self, "Import", "No sequence parsed.")
            return
        
        # Check database engine
        try:
            db_manager.get_engine()
        except Exception:
            QMessageBox.critical(self, "Import Error", "No database connection.")
            return

        delete_existing = False
        existing_id = self.viewmodel.get_existing_id()

        if self.model.target_run_id is not None:
            reply = QMessageBox.question(
                self, "Replace Signal Data",
                f"Delete all existing signal data for run #{self.model.target_run_id} and re-import?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self._status_lbl.setText("Import cancelled.")
                return
            delete_existing = True
        elif existing_id is not None:
            reply = QMessageBox.question(
                self, "Sequence Already Exists",
                f"Sequence ID {existing_id} already imported. Replace?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self._status_lbl.setText("Import cancelled.")
                return
            self.model.target_run_id = existing_id
            delete_existing = True

        total_steps = (
            1
            + len(self.model.sequence.inlets)
            + sum(len(p.ms_data) for p in self.model.sequence.inlets)
        )
        self._progress_dialog = QProgressDialog("Importing sequence…", "Cancel", 0, total_steps + 1, self)
        self._progress_dialog.setWindowModality(Qt.WindowModal)
        self._progress_dialog.setMinimumDuration(0)
        self._progress_dialog.setValue(0)

        self.viewmodel.import_data(delete_existing=delete_existing)

    def _on_progress_updated(self, step: int, message: str) -> None:
        if self._progress_dialog:
            self._progress_dialog.setLabelText(message)
            self._progress_dialog.setValue(step)
            QApplication.processEvents()

    def _on_import_finished(self, seq_id: int) -> None:
        if self._progress_dialog:
            self._progress_dialog.setValue(self._progress_dialog.maximum())
            self._progress_dialog.close()
        
        self._flash_status(f"Imported sequence {seq_id} successfully.")
        QMessageBox.information(
            self, "Import Successful",
            f"Imported sequence {seq_id}:\n"
            f"  Import Completed Successfully."
        )

    def _on_import_failed(self, error_msg: str) -> None:
        if self._progress_dialog:
            self._progress_dialog.close()
        self._flash_status("Import failed.", is_success=False)
        QMessageBox.critical(self, "Import Error", f"Database import failed:\n{error_msg}")

    def _flash_status(self, message: str, is_success: bool = True) -> None:
        # Stop any running fade timers/animations to avoid collisions
        if hasattr(self, "_fade_timer") and self._fade_timer.isActive():
            self._fade_timer.stop()
        if hasattr(self, "_delay_timer") and self._delay_timer.isActive():
            self._delay_timer.stop()

        self._status_lbl.setText(message)
        if not is_success:
            # For error, flash red and stay red
            self._status_lbl.setStyleSheet(
                "QLabel { color: white; background-color: #C62828; font-size: 12px;"
                "  font-weight: bold; padding: 4px 6px; border-top: 1px solid #CFD8DC; border-radius: 4px; }"
            )
            return

        # Flash green
        self._status_lbl.setStyleSheet(
            "QLabel { color: white; background-color: #2E7D32; font-size: 12px;"
            "  font-weight: bold; padding: 4px 6px; border-top: 1px solid #CFD8DC; border-radius: 4px; }"
        )

        start_fg = (255, 255, 255)
        end_fg = (84, 110, 122) # #546E7A
        
        steps = 20
        self._fade_step = 0
        
        def do_fade():
            self._fade_step += 1
            if self._fade_step > steps:
                self._fade_timer.stop()
                # Restore original stylesheet exactly
                self._status_lbl.setStyleSheet(
                    "QLabel { color: #546E7A; font-size: 12px; font-weight: bold;"
                    "  padding: 4px 6px; border-top: 1px solid #CFD8DC; background: #FAFAFA; }"
                )
                return
            
            t = self._fade_step / steps
            bg_alpha = int(255 * (1 - t))
            
            fg_r = int(start_fg[0] + (end_fg[0] - start_fg[0]) * t)
            fg_g = int(start_fg[1] + (end_fg[1] - start_fg[1]) * t)
            fg_b = int(start_fg[2] + (end_fg[2] - start_fg[2]) * t)
            
            self._status_lbl.setStyleSheet(
                f"QLabel {{ color: rgb({fg_r}, {fg_g}, {fg_b}); "
                f"background-color: rgba(46, 125, 50, {bg_alpha}); "
                f"font-size: 12px; font-weight: bold; padding: 4px 6px; "
                f"border-top: 1px solid #CFD8DC; border-radius: 4px; }}"
            )

        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(50) # 50ms * 20 = 1000ms fade duration
        self._fade_timer.timeout.connect(do_fade)
        
        # Start the fade after 1.5 seconds of green visibility
        self._delay_timer = QTimer(self)
        self._delay_timer.setSingleShot(True)
        self._delay_timer.setInterval(1500)
        self._delay_timer.timeout.connect(self._fade_timer.start)
        self._delay_timer.start()
