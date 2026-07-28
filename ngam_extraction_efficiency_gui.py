"""
ngam_extraction_efficiency_gui.py
==================================
Management dialog for ngam.ngextractionlineefficiency.

Allows lab staff to enter, edit, and retire per-element extraction
efficiency (η) calibrations for each noble gas vacuum line / instrument.

Each calibration record covers one noble gas element for one instrument
over a date range (valid_from → valid_until).  The processor resolves the
most recent active record when correcting dissolved concentrations.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QLineEdit, QComboBox, QDoubleSpinBox,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QMessageBox, QCheckBox, QDateTimeEdit,
    QTextEdit, QFrame, QSizePolicy,
)
from PyQt5.QtCore import Qt, QDateTime
from PyQt5.QtGui import QColor, QBrush

from db_core import db_manager
from sqlalchemy import text
from shared_utils import (
    check_employee_privilege, normalize_login_name, get_current_user_id,
)
from gui_utils import show_message

log = logging.getLogger(__name__)

# ─────────────────────────────── palette ─────────────────────────────────────
_HDR_BG   = "#1F3A5F"
_PANEL_BG = "#EAF0F6"

_COL_ACTIVE  = QColor("#E8F5E9")   # green-tinted — currently valid
_COL_EXPIRED = QColor("#FAFAFA")   # neutral — valid_until in the past
_COL_FUTURE  = QColor("#E3F2FD")   # blue-tinted — valid_from in the future

_ELEMENTS = ["He", "Ne", "Ar", "Kr", "Xe"]
_METHODS  = ["double_extraction", "theoretical", "standard", "other"]

# ─────────────────────────────── column indices ───────────────────────────────
class C:
    ID         = 0
    EQUIPMENT  = 1
    ELEMENT    = 2
    VALID_FROM = 3
    VALID_UNTIL= 4
    EFFICIENCY = 5
    UNC        = 6
    METHOD     = 7
    NOTES      = 8
    STATUS     = 9

    HEADERS = [
        "ID", "Equipment", "Element",
        "Valid From", "Valid Until",
        "η", "±η",
        "Method", "Notes", "Status",
    ]


# ═════════════════════════════════════════════════════════════════════════════
# Add / Edit sub-dialog
# ═════════════════════════════════════════════════════════════════════════════

class _EfficiencyEditDialog(QDialog):
    """Form dialog for creating or editing a single efficiency record."""

    def __init__(self, equipment_options: list, record: dict = None, parent=None):
        super().__init__(parent)
        self._record = record   # None → new record
        self._equipment_options = equipment_options  # [(equipmentid, name), ...]
        self.setWindowTitle("Add Efficiency Calibration" if record is None
                            else "Edit Efficiency Calibration")
        self.setMinimumWidth(520)
        self._build_ui()
        if record:
            self._populate(record)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 10)
        root.setSpacing(10)

        grp = QGroupBox("Calibration Details")
        grp.setStyleSheet(f"QGroupBox {{ background:{_PANEL_BG}; }}")
        lay = QGridLayout(grp)
        lay.setSpacing(8)

        def lbl(t):
            l = QLabel(t)
            l.setStyleSheet("font-weight:bold; color:#1F3A5F;")
            l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return l

        # Equipment
        self._cmb_equipment = QComboBox()
        self._cmb_equipment.addItem("— Any instrument (lab-wide) —", None)
        for eid, ename in self._equipment_options:
            self._cmb_equipment.addItem(ename, eid)
        lay.addWidget(lbl("Instrument:"), 0, 0)
        lay.addWidget(self._cmb_equipment, 0, 1, 1, 3)

        # Element
        self._cmb_element = QComboBox()
        for el in _ELEMENTS:
            self._cmb_element.addItem(el, el)
        lay.addWidget(lbl("Element:"), 1, 0)
        lay.addWidget(self._cmb_element, 1, 1)

        # Method
        self._cmb_method = QComboBox()
        self._cmb_method.addItem("— not specified —", None)
        for m in _METHODS:
            self._cmb_method.addItem(m.replace("_", " ").capitalize(), m)
        lay.addWidget(lbl("Method:"), 1, 2)
        lay.addWidget(self._cmb_method, 1, 3)

        # Valid From / Until
        def _dt_widget():
            w = QDateTimeEdit()
            w.setDisplayFormat("yyyy-MM-dd HH:mm")
            w.setCalendarPopup(True)
            w.setDateTime(QDateTime.currentDateTime())
            w.setMinimumDateTime(QDateTime(1990, 1, 1, 0, 0))
            return w

        self._dt_from  = _dt_widget()
        self._dt_until = _dt_widget()
        self._chk_no_until = QCheckBox("Still current (no end date)")
        self._chk_no_until.setChecked(True)
        self._chk_no_until.toggled.connect(self._dt_until.setDisabled)
        self._dt_until.setEnabled(False)

        lay.addWidget(lbl("Valid From:"), 2, 0)
        lay.addWidget(self._dt_from, 2, 1)
        lay.addWidget(lbl("Valid Until:"), 2, 2)
        lay.addWidget(self._dt_until, 2, 3)
        lay.addWidget(self._chk_no_until, 3, 1, 1, 3)

        # η and ±η
        self._spin_eta = QDoubleSpinBox()
        self._spin_eta.setRange(0.001, 1.000)
        self._spin_eta.setSingleStep(0.01)
        self._spin_eta.setDecimals(4)
        self._spin_eta.setValue(1.0)
        self._spin_eta.setToolTip("Fractional extraction efficiency (0 < η ≤ 1.0)")

        self._spin_unc = QDoubleSpinBox()
        self._spin_unc.setRange(0.0, 1.0)
        self._spin_unc.setSingleStep(0.001)
        self._spin_unc.setDecimals(4)
        self._spin_unc.setValue(0.0)
        self._spin_unc.setToolTip("1-sigma uncertainty on η (0 = not specified)")
        self._chk_no_unc = QCheckBox("No uncertainty")
        self._chk_no_unc.setChecked(True)
        self._chk_no_unc.toggled.connect(self._spin_unc.setDisabled)
        self._spin_unc.setEnabled(False)

        lay.addWidget(lbl("η:"), 4, 0)
        lay.addWidget(self._spin_eta, 4, 1)
        lay.addWidget(lbl("±η:"), 4, 2)
        lay.addWidget(self._spin_unc, 4, 3)
        lay.addWidget(self._chk_no_unc, 5, 2, 1, 2)

        # Notes
        self._txt_notes = QTextEdit()
        self._txt_notes.setFixedHeight(60)
        self._txt_notes.setPlaceholderText(
            "Optional: describe the measurement setup, reference used, etc."
        )
        lay.addWidget(lbl("Notes:"), 6, 0, Qt.AlignTop)
        lay.addWidget(self._txt_notes, 6, 1, 1, 3)

        lay.setColumnStretch(1, 1)
        lay.setColumnStretch(3, 1)
        root.addWidget(grp)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._btn_ok = QPushButton("Save")
        self._btn_ok.setStyleSheet(
            "QPushButton { background:#2E7D32; color:white; font-weight:bold;"
            "  border:none; padding:5px 20px; border-radius:4px; }"
            "QPushButton:hover { background:#1B5E20; }"
        )
        self._btn_ok.clicked.connect(self._on_save)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setStyleSheet(
            "QPushButton { background:#7f8c8d; color:white; font-weight:bold;"
            "  border:none; padding:5px 20px; border-radius:4px; }"
            "QPushButton:hover { background:#636e72; }"
        )
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(self._btn_ok)
        root.addLayout(btn_row)

    def _populate(self, r: dict):
        # Equipment
        eq_id = r.get("equipmentid")
        idx = self._cmb_equipment.findData(eq_id)
        if idx >= 0:
            self._cmb_equipment.setCurrentIndex(idx)
        # Element
        idx = self._cmb_element.findData(r.get("element", "He"))
        if idx >= 0:
            self._cmb_element.setCurrentIndex(idx)
        # Method
        idx = self._cmb_method.findData(r.get("method"))
        if idx >= 0:
            self._cmb_method.setCurrentIndex(idx)
        # Dates
        if r.get("valid_from"):
            dt = r["valid_from"]
            self._dt_from.setDateTime(
                QDateTime(dt.year, dt.month, dt.day, dt.hour, dt.minute)
            )
        if r.get("valid_until"):
            dt = r["valid_until"]
            self._dt_until.setDateTime(
                QDateTime(dt.year, dt.month, dt.day, dt.hour, dt.minute)
            )
            self._chk_no_until.setChecked(False)
        # η / ±η
        if r.get("efficiency") is not None:
            self._spin_eta.setValue(float(r["efficiency"]))
        if r.get("efficiency_unc") is not None:
            self._spin_unc.setValue(float(r["efficiency_unc"]))
            self._chk_no_unc.setChecked(False)
        # Notes
        self._txt_notes.setPlainText(r.get("notes") or "")

    def _on_save(self):
        eta = self._spin_eta.value()
        if not (0 < eta <= 1.0):
            show_message(self, "Validation Error",
                         "η must be between 0 (exclusive) and 1.0 (inclusive).",
                         QMessageBox.Warning)
            return
        self.accept()

    def get_values(self) -> dict:
        unc = None if self._chk_no_unc.isChecked() else self._spin_unc.value()
        until = None if self._chk_no_until.isChecked() \
            else self._dt_until.dateTime().toPyDateTime()
        return {
            "equipmentid": self._cmb_equipment.currentData(),
            "element":     self._cmb_element.currentData(),
            "method":      self._cmb_method.currentData(),
            "valid_from":  self._dt_from.dateTime().toPyDateTime(),
            "valid_until": until,
            "efficiency":  self._spin_eta.value(),
            "efficiency_unc": unc,
            "notes": self._txt_notes.toPlainText().strip() or None,
        }


# ═════════════════════════════════════════════════════════════════════════════
# Main management dialog
# ═════════════════════════════════════════════════════════════════════════════

class ExtractionEfficiencyDialog(QDialog):
    """
    Master view of all extraction line efficiency calibration records.

    Filter bar  → filter by instrument and/or element; toggle expired.
    Table       → one row per record; colour-coded by status.
    Actions     → Add, Edit, Retire (set valid_until), Delete.
    """

    def __init__(self, parent=None, *, equipment_id=None, run_id=None):
        super().__init__(parent)
        self.setWindowTitle("Extraction Line Efficiency Calibrations")
        self.resize(1100, 640)
        self.setMinimumSize(820, 480)
        self._preset_equipment_id = equipment_id
        self._preset_run_id = run_id
        self._equipment_options: list = []   # [(id, name), ...]
        self._all_rows: list = []            # raw dicts from DB
        self._check_privileges()
        self._build_ui()
        self._load_equipment()
        self._load_records()

    # ── privileges ────────────────────────────────────────────────────────────

    def _check_privileges(self):
        try:
            user = normalize_login_name(get_current_user_id())
            self.has_write = check_employee_privilege(user, "ngamaccess")
        except Exception:
            self.has_write = False

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header bar
        hdr_w = QFrame()
        hdr_w.setFixedHeight(40)
        hdr_w.setStyleSheet(f"background:{_HDR_BG};")
        hdr_lay = QHBoxLayout(hdr_w)
        hdr_lay.setContentsMargins(12, 4, 12, 4)
        title = QLabel("  Extraction Line Efficiency Calibrations")
        title.setStyleSheet(
            "color:white; font-weight:bold; font-size:13px; background:transparent;"
        )
        hdr_lay.addWidget(title, 1)
        root.addWidget(hdr_w)

        body = QVBoxLayout()
        body.setContentsMargins(10, 8, 10, 8)
        body.setSpacing(8)
        root.addLayout(body, 1)

        # Filter strip
        filter_frm = QFrame()
        filter_frm.setStyleSheet(
            f"QFrame {{ background:{_PANEL_BG}; border:1px solid #BCC8D8;"
            f"  border-radius:4px; }}"
        )
        filter_lay = QHBoxLayout(filter_frm)
        filter_lay.setContentsMargins(10, 6, 10, 6)
        filter_lay.setSpacing(10)

        filter_lay.addWidget(QLabel("Instrument:"))
        self._cmb_filter_equip = QComboBox()
        self._cmb_filter_equip.setFixedWidth(200)
        self._cmb_filter_equip.addItem("All instruments", None)
        self._cmb_filter_equip.currentIndexChanged.connect(self._apply_filter)
        filter_lay.addWidget(self._cmb_filter_equip)

        filter_lay.addWidget(QLabel("Element:"))
        self._cmb_filter_elem = QComboBox()
        self._cmb_filter_elem.setFixedWidth(80)
        self._cmb_filter_elem.addItem("All", None)
        for el in _ELEMENTS:
            self._cmb_filter_elem.addItem(el, el)
        self._cmb_filter_elem.currentIndexChanged.connect(self._apply_filter)
        filter_lay.addWidget(self._cmb_filter_elem)

        self._chk_show_expired = QCheckBox("Show expired")
        self._chk_show_expired.setChecked(False)
        self._chk_show_expired.toggled.connect(self._apply_filter)
        filter_lay.addWidget(self._chk_show_expired)

        filter_lay.addStretch()

        self._lbl_count = QLabel("")
        self._lbl_count.setStyleSheet("color:#546E7A; font-size:11px;")
        filter_lay.addWidget(self._lbl_count)

        body.addWidget(filter_frm)

        # Table
        self._tbl = QTableWidget()
        self._tbl.setColumnCount(len(C.HEADERS))
        self._tbl.setHorizontalHeaderLabels(C.HEADERS)
        self._tbl.setShowGrid(False)
        self._tbl.setAlternatingRowColors(False)
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._tbl.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setStyleSheet("""
            QTableWidget { border: none; background: white; }
            QTableWidget::item { padding: 4px 6px; }
            QTableWidget::item:selected { background: #DDEEFF; color: #000; }
            QHeaderView::section {
                background: #1F3A5F; color: white; font-weight: bold;
                padding: 4px 6px; border: 1px solid #0F2A4F;
            }
        """)
        hdr = self._tbl.horizontalHeader()
        hdr.setSectionResizeMode(C.ID,          QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(C.EQUIPMENT,   QHeaderView.Stretch)
        hdr.setSectionResizeMode(C.ELEMENT,     QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(C.VALID_FROM,  QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(C.VALID_UNTIL, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(C.EFFICIENCY,  QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(C.UNC,         QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(C.METHOD,      QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(C.NOTES,       QHeaderView.Stretch)
        hdr.setSectionResizeMode(C.STATUS,      QHeaderView.ResizeToContents)
        self._tbl.doubleClicked.connect(self._on_edit)
        body.addWidget(self._tbl, 1)

        # Legend
        legend_lay = QHBoxLayout()
        legend_lay.setSpacing(16)
        for color, label in [
            (_COL_ACTIVE,  "Active"),
            (_COL_FUTURE,  "Future"),
            (_COL_EXPIRED, "Expired"),
        ]:
            dot = QLabel("■")
            dot.setStyleSheet(f"color: {color.name()}; font-size:18px;")
            legend_lay.addWidget(dot)
            legend_lay.addWidget(QLabel(label))
        legend_lay.addStretch()
        body.addLayout(legend_lay)

        # Action buttons
        btn_frm = QFrame()
        btn_frm.setStyleSheet(
            f"background:{_PANEL_BG}; border-top:1px solid #BCC8D8;"
        )
        btn_lay = QHBoxLayout(btn_frm)
        btn_lay.setContentsMargins(10, 6, 10, 6)
        btn_lay.setSpacing(8)

        def _btn(label, color, hover):
            b = QPushButton(label)
            b.setFixedHeight(30)
            b.setStyleSheet(
                f"QPushButton {{ background:{color}; color:white; font-weight:bold;"
                f"  border:none; padding:4px 16px; border-radius:4px; }}"
                f"QPushButton:hover {{ background:{hover}; }}"
                f"QPushButton:disabled {{ background:#90A4AE; color:#CFD8DC; }}"
            )
            return b

        self._btn_add    = _btn("Add",    "#2E7D32", "#1B5E20")
        self._btn_edit   = _btn("Edit",   "#1565C0", "#0D47A1")
        self._btn_retire = _btn("Retire", "#E65100", "#BF360C")
        self._btn_delete = _btn("Delete", "#7f8c8d", "#636e72")
        btn_close        = _btn("Close",  "#455A64", "#37474F")

        self._btn_add.setToolTip("Add a new efficiency calibration record")
        self._btn_edit.setToolTip("Edit the selected record")
        self._btn_retire.setToolTip(
            "Set valid_until = now() to retire the selected record\n"
            "without deleting it (preserves audit trail)"
        )
        self._btn_delete.setToolTip("Permanently delete the selected record")

        self._btn_add.clicked.connect(self._on_add)
        self._btn_edit.clicked.connect(self._on_edit)
        self._btn_retire.clicked.connect(self._on_retire)
        self._btn_delete.clicked.connect(self._on_delete)
        btn_close.clicked.connect(self.reject)

        for b in (self._btn_add, self._btn_edit, self._btn_retire,
                  self._btn_delete):
            b.setEnabled(self.has_write)

        btn_lay.addStretch()
        for b in (self._btn_add, self._btn_edit, self._btn_retire,
                  self._btn_delete, btn_close):
            btn_lay.addWidget(b)

        root.addWidget(btn_frm)

    # ── data loading ──────────────────────────────────────────────────────────

    def _load_equipment(self):
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT equipmentid, equipmentname FROM public.equipment"
                    " ORDER BY equipmentname"
                )).fetchall()
            self._equipment_options = [(r[0], r[1]) for r in rows]
            for eid, ename in self._equipment_options:
                self._cmb_filter_equip.addItem(ename, eid)
            if self._preset_equipment_id is not None:
                for i in range(self._cmb_filter_equip.count()):
                    if self._cmb_filter_equip.itemData(i) == self._preset_equipment_id:
                        self._cmb_filter_equip.setCurrentIndex(i)
                        break
        except Exception as e:
            log.error(f"Equipment load failed: {e}")

    def _load_records(self):
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT
                        e.efficiencyid, e.equipmentid,
                        eq.equipmentname,
                        e.element,
                        e.valid_from, e.valid_until,
                        e.efficiency, e.efficiency_unc,
                        e.method, e.notes,
                        e.createdatestamp, e.createuserstamp
                    FROM ngam.ngextractionlineefficiency e
                    LEFT JOIN public.equipment eq
                        ON eq.equipmentid = e.equipmentid
                    ORDER BY e.element, e.valid_from DESC
                """)).fetchall()
        except Exception as e:
            log.error(f"Efficiency records load failed: {e}")
            show_message(self, "Database Error",
                         f"Could not load efficiency records:\n{e}",
                         QMessageBox.Critical)
            return

        self._all_rows = [
            {
                "efficiencyid":   r[0],
                "equipmentid":    r[1],
                "equipmentname":  r[2] or "— any —",
                "element":        r[3],
                "valid_from":     r[4],
                "valid_until":    r[5],
                "efficiency":     r[6],
                "efficiency_unc": r[7],
                "method":         r[8],
                "notes":          r[9],
                "createdate":     r[10],
                "createuser":     r[11],
            }
            for r in rows
        ]
        self._apply_filter()

    def _apply_filter(self):
        equip_filter = self._cmb_filter_equip.currentData()
        elem_filter  = self._cmb_filter_elem.currentData()
        show_expired = self._chk_show_expired.isChecked()
        now = datetime.now(timezone.utc)

        visible = []
        for r in self._all_rows:
            if equip_filter is not None and r["equipmentid"] != equip_filter:
                continue
            if elem_filter is not None and r["element"] != elem_filter:
                continue
            until = r["valid_until"]
            if until is not None:
                # Make aware if naive
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if until <= now and not show_expired:
                    continue
            visible.append(r)

        self._fill_table(visible)
        self._lbl_count.setText(f"{len(visible)} record(s) shown")

    def _fill_table(self, records: list):
        self._tbl.setRowCount(0)
        now = datetime.now(timezone.utc)

        def _fmt_dt(dt):
            if dt is None:
                return "—"
            return dt.strftime("%Y-%m-%d %H:%M") if hasattr(dt, "strftime") else str(dt)

        for r in records:
            row = self._tbl.rowCount()
            self._tbl.insertRow(row)

            # Determine status and row colour
            vf = r["valid_from"]
            vu = r["valid_until"]
            if vf is not None and vf.tzinfo is None:
                vf = vf.replace(tzinfo=timezone.utc)
            if vu is not None and vu.tzinfo is None:
                vu = vu.replace(tzinfo=timezone.utc)

            if vf is not None and vf > now:
                status = "Future"
                bg = _COL_FUTURE
            elif vu is not None and vu <= now:
                status = "Expired"
                bg = _COL_EXPIRED
            else:
                status = "Active"
                bg = _COL_ACTIVE

            unc_str = (f"±{r['efficiency_unc']:.4f}"
                       if r["efficiency_unc"] is not None else "—")
            method_str = (r["method"].replace("_", " ").capitalize()
                          if r["method"] else "—")

            cells = [
                QTableWidgetItem(str(r["efficiencyid"])),
                QTableWidgetItem(r["equipmentname"]),
                QTableWidgetItem(r["element"] or ""),
                QTableWidgetItem(_fmt_dt(r["valid_from"])),
                QTableWidgetItem(_fmt_dt(r["valid_until"])),
                QTableWidgetItem(f"{r['efficiency']:.4f}"),
                QTableWidgetItem(unc_str),
                QTableWidgetItem(method_str),
                QTableWidgetItem(r["notes"] or ""),
                QTableWidgetItem(status),
            ]
            brush = QBrush(bg)
            for c, item in enumerate(cells):
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                item.setBackground(brush)
                if c in (C.ID, C.ELEMENT, C.EFFICIENCY, C.UNC, C.STATUS):
                    item.setTextAlignment(Qt.AlignCenter)
                # Store efficiencyid in UserRole of first cell
                if c == C.ID:
                    item.setData(Qt.UserRole, r["efficiencyid"])
                self._tbl.setItem(row, c, item)

    # ── selected row helpers ──────────────────────────────────────────────────

    def _selected_id(self) -> Optional[int]:
        rows = self._tbl.selectedItems()
        if not rows:
            return None
        return self._tbl.item(self._tbl.currentRow(), C.ID).data(Qt.UserRole)

    def _selected_record(self) -> Optional[dict]:
        eid = self._selected_id()
        if eid is None:
            return None
        return next((r for r in self._all_rows if r["efficiencyid"] == eid), None)

    # ── actions ───────────────────────────────────────────────────────────────

    def _on_add(self):
        dlg = _EfficiencyEditDialog(self._equipment_options, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        vals = dlg.get_values()
        try:
            user_stamp = normalize_login_name(get_current_user_id())
        except Exception:
            user_stamp = "unknown"
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    INSERT INTO ngam.ngextractionlineefficiency
                        (equipmentid, element, valid_from, valid_until,
                         efficiency, efficiency_unc, method, notes,
                         createuserstamp)
                    VALUES
                        (:eq, :el, :vf, :vu,
                         :eta, :unc, :meth, :notes,
                         :user)
                """), {
                    "eq":    vals["equipmentid"],
                    "el":    vals["element"],
                    "vf":    vals["valid_from"],
                    "vu":    vals["valid_until"],
                    "eta":   vals["efficiency"],
                    "unc":   vals["efficiency_unc"],
                    "meth":  vals["method"],
                    "notes": vals["notes"],
                    "user":  user_stamp,
                })
                conn.commit()
            self._load_records()
        except Exception as e:
            log.error(f"Insert efficiency record failed: {e}")
            show_message(self, "Database Error", str(e), QMessageBox.Critical)

    def _on_edit(self):
        rec = self._selected_record()
        if rec is None:
            show_message(self, "No Selection",
                         "Select a record to edit.", QMessageBox.Information)
            return
        dlg = _EfficiencyEditDialog(self._equipment_options, record=rec, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        vals = dlg.get_values()
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE ngam.ngextractionlineefficiency
                    SET equipmentid    = :eq,
                        element        = :el,
                        valid_from     = :vf,
                        valid_until    = :vu,
                        efficiency     = :eta,
                        efficiency_unc = :unc,
                        method         = :meth,
                        notes          = :notes
                    WHERE efficiencyid = :eid
                """), {
                    "eq":   vals["equipmentid"],
                    "el":   vals["element"],
                    "vf":   vals["valid_from"],
                    "vu":   vals["valid_until"],
                    "eta":  vals["efficiency"],
                    "unc":  vals["efficiency_unc"],
                    "meth": vals["method"],
                    "notes":vals["notes"],
                    "eid":  rec["efficiencyid"],
                })
                conn.commit()
            self._load_records()
        except Exception as e:
            log.error(f"Update efficiency record failed: {e}")
            show_message(self, "Database Error", str(e), QMessageBox.Critical)

    def _on_retire(self):
        rec = self._selected_record()
        if rec is None:
            show_message(self, "No Selection",
                         "Select a record to retire.", QMessageBox.Information)
            return
        reply = QMessageBox.question(
            self, "Retire Record",
            f"Set valid_until = now() for record #{rec['efficiencyid']} "
            f"({rec['element']} η = {rec['efficiency']:.4f})?\n\n"
            "The record will be preserved for audit purposes.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE ngam.ngextractionlineefficiency
                    SET valid_until = now()
                    WHERE efficiencyid = :eid
                      AND (valid_until IS NULL OR valid_until > now())
                """), {"eid": rec["efficiencyid"]})
                conn.commit()
            self._load_records()
        except Exception as e:
            log.error(f"Retire efficiency record failed: {e}")
            show_message(self, "Database Error", str(e), QMessageBox.Critical)

    def _on_delete(self):
        rec = self._selected_record()
        if rec is None:
            show_message(self, "No Selection",
                         "Select a record to delete.", QMessageBox.Information)
            return
        reply = QMessageBox.question(
            self, "Delete Record",
            f"Permanently delete efficiency record #{rec['efficiencyid']}?\n\n"
            "Consider using Retire instead to preserve the audit trail.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    DELETE FROM ngam.ngextractionlineefficiency
                    WHERE efficiencyid = :eid
                """), {"eid": rec["efficiencyid"]})
                conn.commit()
            self._load_records()
        except Exception as e:
            log.error(f"Delete efficiency record failed: {e}")
            show_message(self, "Database Error", str(e), QMessageBox.Critical)


# ─── standalone test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from PyQt5.QtWidgets import QApplication
    logging.basicConfig(level=logging.DEBUG)
    app = QApplication(sys.argv)
    dlg = ExtractionEfficiencyDialog()
    dlg.show()
    sys.exit(app.exec_())
