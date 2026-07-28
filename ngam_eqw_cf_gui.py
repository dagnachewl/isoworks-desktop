"""
ngam_eqw_cf_gui.py
==================
NGAM – EQW Correction Factor Management

Displays all EQW runs (ng_eqw_run) for a selected (container type, instrument)
combination.  Each row shows per-gas correction factors with individual lock
toggles.  The aggregate mean CF and 1σ% are shown in a summary strip.

Workflow
--------
1. Filter: choose Container Type + Instrument in the toolbar.
2. Review: table shows each EQW run with per-gas CFs and lock checkboxes.
   Toggle locks to exclude outliers; the summary strip updates live.
3. Create template: "Create Template" button (admin only) computes the aggregate
   CFs from unlocked rows and records a new ng_cf_template row.
4. Promote: "Promote to Applied" button sets is_current = TRUE on the new
   template, making those CFs the active correction for data reduction.

Admin privilege required for Create Template and Promote.
"""
from __future__ import annotations

import getpass
import logging
import math
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont, QStandardItem, QStandardItemModel
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSizePolicy, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
    QWidget, QComboBox, QFrame, QSplitter,
)

from db_core import db_manager
from gui_utils import show_message
from help_browser import make_help_button
from shared_utils import check_employee_privilege, normalize_login_name, get_current_user_id
from sqlalchemy import text
from ngam_eqw_processor import GASES, aggregate_cfs

log = logging.getLogger(__name__)

# ── palette ──────────────────────────────────────────────────────────────────
_HDR_BG   = "#1F3A5F"
_HDR_FG   = "white"
_LOCK_COL = QColor(255, 220, 220)   # light red — locked row
_GOOD_COL = QColor(230, 255, 230)   # light green — unlocked

# Column indices in the EQW runs table
class C:
    RUN_ID   = 0
    AID      = 1
    LABID    = 2
    TEMP     = 3
    PRESS    = 4
    SAL      = 5
    # per-gas CF + lock  (pairs: CF value then lock checkbox)
    HE_CF    = 6;   HE_LOCK  = 7
    NE_CF    = 8;   NE_LOCK  = 9
    AR_CF    = 10;  AR_LOCK  = 11
    KR_CF    = 12;  KR_LOCK  = 13
    XE_CF    = 14;  XE_LOCK  = 15
    CREATED  = 16

    HEADERS = [
        "Run ID", "AnalysisID", "Lab ID",
        "Temp (°C)", "Press (Torr)", "Sal (‰)",
        "CF He", "Lock He",
        "CF Ne", "Lock Ne",
        "CF Ar", "Lock Ar",
        "CF Kr", "Lock Kr",
        "CF Xe", "Lock Xe",
        "Created",
    ]

    CF_COLS   = [HE_CF,  NE_CF,  AR_CF,  KR_CF,  XE_CF]
    LOCK_COLS = [HE_LOCK, NE_LOCK, AR_LOCK, KR_LOCK, XE_LOCK]

    @staticmethod
    def cf_col(gas: str) -> int:
        return [C.HE_CF, C.NE_CF, C.AR_CF, C.KR_CF, C.XE_CF][GASES.index(gas)]

    @staticmethod
    def lock_col(gas: str) -> int:
        return [C.HE_LOCK, C.NE_LOCK, C.AR_LOCK, C.KR_LOCK, C.XE_LOCK][GASES.index(gas)]


# ─────────────────────────────────────────────────────────────────────────────
# Summary strip widget
# ─────────────────────────────────────────────────────────────────────────────

class _SummaryStrip(QFrame):
    """Horizontal strip displaying one column per gas: CF ± err%."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("background:#1F3A5F; border-radius:4px;")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)

        hdr = QLabel("Computed CFs:")
        hdr.setStyleSheet("color:white; font-weight:bold;")
        lay.addWidget(hdr)
        lay.addSpacing(12)

        self._labels: Dict[str, Tuple[QLabel, QLabel]] = {}
        for g in GASES:
            box = QVBoxLayout()
            top = QLabel(g)
            top.setStyleSheet("color:#90CAF9; font-size:10px; font-weight:bold;")
            top.setAlignment(Qt.AlignCenter)
            val = QLabel("—")
            val.setStyleSheet("color:white; font-size:11px;")
            val.setAlignment(Qt.AlignCenter)
            box.addWidget(top)
            box.addWidget(val)
            lay.addLayout(box)
            lay.addSpacing(8)
            self._labels[g] = (top, val)

        lay.addStretch()

        lbl_applied = QLabel("Applied CFs:")
        lbl_applied.setStyleSheet("color:#A5D6A7; font-weight:bold;")
        lay.addWidget(lbl_applied)
        lay.addSpacing(12)

        self._applied_labels: Dict[str, QLabel] = {}
        for g in GASES:
            box = QVBoxLayout()
            top = QLabel(g)
            top.setStyleSheet("color:#A5D6A7; font-size:10px; font-weight:bold;")
            top.setAlignment(Qt.AlignCenter)
            val = QLabel("—")
            val.setStyleSheet("color:#E8F5E9; font-size:11px;")
            val.setAlignment(Qt.AlignCenter)
            box.addWidget(top)
            box.addWidget(val)
            lay.addLayout(box)
            lay.addSpacing(8)
            self._applied_labels[g] = val

    def update_computed(self, agg: Dict[str, Optional[Tuple[float, float]]]):
        for g in GASES:
            _, val_lbl = self._labels[g]
            entry = agg.get(g)
            if entry is None:
                val_lbl.setText("—")
            else:
                mu, rel_err = entry
                err_pct = rel_err * 100 if not math.isnan(rel_err) else float("nan")
                if math.isnan(err_pct):
                    val_lbl.setText(f"{mu:.4f}")
                else:
                    val_lbl.setText(f"{mu:.4f}\n±{err_pct:.2f}%")

    def update_applied(self, applied: Optional[Dict[str, Tuple[float, float]]]):
        for g in GASES:
            lbl = self._applied_labels[g]
            if applied is None or g not in applied:
                lbl.setText("—")
            else:
                cf, err = applied[g]
                err_pct = err * 100 if not math.isnan(err) else float("nan")
                if math.isnan(err_pct):
                    lbl.setText(f"{cf:.4f}")
                else:
                    lbl.setText(f"{cf:.4f}\n±{err_pct:.2f}%")


# ─────────────────────────────────────────────────────────────────────────────
# Promote dialog
# ─────────────────────────────────────────────────────────────────────────────

class _PromoteDialog(QDialog):
    """Confirm promotion: set computed CFs as applied + optional notes."""

    def __init__(self, agg: Dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Promote CFs to Applied")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)

        strip = QLabel("  Set the computed CFs as the new Applied CFs for this template?")
        strip.setFixedHeight(30)
        strip.setStyleSheet(
            "background:#1B5E20; color:white; font-weight:bold; font-size:12px;"
        )
        lay.addWidget(strip)

        tbl = QTableWidget(len(GASES), 3)
        tbl.setHorizontalHeaderLabels(["Gas", "CF", "Error (%)"])
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.verticalHeader().setVisible(False)
        for i, g in enumerate(GASES):
            tbl.setItem(i, 0, QTableWidgetItem(g))
            entry = agg.get(g)
            if entry:
                mu, rel = entry
                err_pct = rel * 100 if not math.isnan(rel) else float("nan")
                tbl.setItem(i, 1, QTableWidgetItem(f"{mu:.6f}"))
                tbl.setItem(i, 2, QTableWidgetItem(
                    f"{err_pct:.3f}%" if not math.isnan(err_pct) else "—"
                ))
            else:
                tbl.setItem(i, 1, QTableWidgetItem("—"))
                tbl.setItem(i, 2, QTableWidgetItem("—"))
        lay.addWidget(tbl)

        lay.addWidget(QLabel("Notes (optional):"))
        self._notes = QTextEdit()
        self._notes.setFixedHeight(60)
        lay.addWidget(self._notes)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def notes(self) -> str:
        return self._notes.toPlainText().strip()


# ─────────────────────────────────────────────────────────────────────────────
# Main widget
# ─────────────────────────────────────────────────────────────────────────────

class EqwCFWidget(QWidget):
    """
    Embeddable widget — shows all EQW runs for a (container_type, equipment)
    pair and lets an admin analyst manage CFs.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._run_rows: List[dict] = []    # raw DB rows, parallel to table
        self._has_admin = False
        self._current_template_id: Optional[int] = None
        self._build_ui()
        self._load_combos()
        self._check_admin()

    # ── build UI ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        # ── filter bar ──────────────────────────────────────────────────────
        fbar = QHBoxLayout()
        fbar.addWidget(QLabel("Container Type:"))
        self._cmb_ctype = QComboBox(); self._cmb_ctype.setMinimumWidth(180)
        fbar.addWidget(self._cmb_ctype)
        fbar.addSpacing(16)
        fbar.addWidget(QLabel("Instrument:"))
        self._cmb_equip = QComboBox(); self._cmb_equip.setMinimumWidth(200)
        fbar.addWidget(self._cmb_equip)
        fbar.addSpacing(16)
        self._btn_load = QPushButton("Load EQW Runs")
        self._btn_load.clicked.connect(self._load_runs)
        fbar.addWidget(self._btn_load)
        fbar.addStretch()
        fbar.addWidget(make_help_button(self, "ngam_eqw_cf"))
        root.addLayout(fbar)

        # ── summary strip ───────────────────────────────────────────────────
        self._summary = _SummaryStrip()
        root.addWidget(self._summary)

        # ── runs table ──────────────────────────────────────────────────────
        self._tbl = QTableWidget(0, len(C.HEADERS))
        self._tbl.setHorizontalHeaderLabels(C.HEADERS)
        hdr = self._tbl.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(C.LABID, QHeaderView.Stretch)
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._tbl.setSelectionMode(QAbstractItemView.MultiSelection)
        self._tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.itemChanged.connect(self._on_item_changed)
        root.addWidget(self._tbl, 1)

        # ── template history ─────────────────────────────────────────────────
        hist_grp = QGroupBox("Template History")
        hist_lay = QVBoxLayout(hist_grp)
        self._tbl_hist = QTableWidget(0, 8)
        self._tbl_hist.setHorizontalHeaderLabels([
            "ID", "Serial", "n EQW",
            "CF He", "CF Ne", "CF Ar", "CF Kr", "CF Xe",
        ])
        self._tbl_hist.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._tbl_hist.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._tbl_hist.verticalHeader().setVisible(False)
        self._tbl_hist.setFixedHeight(120)
        hist_lay.addWidget(self._tbl_hist)
        root.addWidget(hist_grp)

        # ── action bar ───────────────────────────────────────────────────────
        abar = QHBoxLayout()
        abar.addStretch()
        self._btn_create = QPushButton("Create Template from Unlocked Runs")
        self._btn_create.setStyleSheet(
            "QPushButton{background:#1565C0;color:white;font-weight:bold;"
            "padding:6px 14px;border-radius:4px;}"
            "QPushButton:hover{background:#1976D2;}"
            "QPushButton:disabled{background:#90A4AE;color:#CFD8DC;}"
        )
        self._btn_create.clicked.connect(self._create_template)
        abar.addWidget(self._btn_create)

        self._btn_promote = QPushButton("Promote to Applied")
        self._btn_promote.setStyleSheet(
            "QPushButton{background:#2E7D32;color:white;font-weight:bold;"
            "padding:6px 14px;border-radius:4px;}"
            "QPushButton:hover{background:#388E3C;}"
            "QPushButton:disabled{background:#90A4AE;color:#CFD8DC;}"
        )
        self._btn_promote.clicked.connect(self._promote)
        abar.addWidget(self._btn_promote)
        root.addLayout(abar)

        self._btn_create.setEnabled(False)
        self._btn_promote.setEnabled(False)

    # ── combos ───────────────────────────────────────────────────────────────

    def _load_combos(self):
        try:
            with db_manager.get_connection() as conn:
                # Only the three NG container types (IDs 1-3)
                ctypes = conn.execute(text("""
                    SELECT container_type_id, type_name
                    FROM   public.container_type_lookup
                    WHERE  container_type_id IN (1, 2, 3)
                      AND  is_active = TRUE
                    ORDER  BY container_type_id
                """)).fetchall()
                # Equipment belonging to NGAM (cat 14) or NGAM_Ingrowth (cat 11)
                equips = conn.execute(text("""
                    SELECT equipmentid, equipmentname
                    FROM   public.equipment
                    WHERE  categoryid IN (11, 14)
                      AND  endoperationdate IS NULL
                    ORDER  BY equipmentname
                """)).fetchall()
        except Exception as exc:
            log.error("Combo load failed: %s", exc)
            return

        self._cmb_ctype.clear()
        for r in ctypes:
            self._cmb_ctype.addItem(str(r[1]), int(r[0]))

        self._cmb_equip.clear()
        for r in equips:
            self._cmb_equip.addItem(str(r[1]), int(r[0]))

    def _check_admin(self):
        try:
            uid = get_current_user_id()
            self._has_admin = check_employee_privilege(uid, "IsAdmin")
        except Exception:
            self._has_admin = False

    # ── load EQW runs ─────────────────────────────────────────────────────────

    def _load_runs(self):
        ctype_id = self._cmb_ctype.currentData()
        equip_id = self._cmb_equip.currentData()
        if ctype_id is None or equip_id is None:
            return

        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT
                        r.eqw_run_id,
                        r.analysisid,
                        COALESCE(a.prefix || '-' || CAST(a.sampleid AS TEXT), ''),
                        r.temperature_c,
                        r.pressure_torr,
                        r.salinity_ppt,
                        r.cf_he,  r.lock_he,
                        r.cf_ne,  r.lock_ne,
                        r.cf_ar,  r.lock_ar,
                        r.cf_kr,  r.lock_kr,
                        r.cf_xe,  r.lock_xe,
                        r.created_at
                    FROM   ngam.ng_eqw_run r
                    LEFT JOIN public.analysis a ON a.analysisid = r.analysisid
                    WHERE  r.container_type_id = :ctype
                      AND  r.equipmentid       = :equip
                    ORDER  BY r.created_at DESC
                """), {"ctype": ctype_id, "equip": equip_id}).fetchall()

                # Load current applied CFs
                from ngam_eqw_processor import get_current_applied_cfs
                applied = get_current_applied_cfs(conn, ctype_id, equip_id)

                # Load template history
                hist = conn.execute(text("""
                    SELECT template_id, serial_number, n_eqw_runs, is_current,
                           applied_cf_he, applied_cf_ne, applied_cf_ar,
                           applied_cf_kr, applied_cf_xe,
                           promoted_at
                    FROM   ngam.ng_cf_template
                    WHERE  container_type_id = :ctype
                      AND  equipmentid       = :equip
                    ORDER  BY serial_number DESC
                """), {"ctype": ctype_id, "equip": equip_id}).fetchall()

        except Exception as exc:
            show_message(self, "Error", str(exc), QMessageBox.Critical)
            return

        self._run_rows = [tuple(r) for r in rows]
        self._populate_table()
        self._summary.update_applied(applied)
        self._populate_history(hist)
        self._recompute_summary()

        has_rows = len(rows) > 0
        self._btn_create.setEnabled(has_rows and self._has_admin)
        self._btn_promote.setEnabled(False)   # enabled only after template created this session
        self._current_template_id = None

    # ── populate runs table ───────────────────────────────────────────────────

    def _populate_table(self):
        self._tbl.blockSignals(True)
        self._tbl.setRowCount(0)

        for r in self._run_rows:
            row = self._tbl.rowCount()
            self._tbl.insertRow(row)

            def _it(val, align=Qt.AlignCenter) -> QTableWidgetItem:
                it = QTableWidgetItem(str(val) if val is not None else "")
                it.setTextAlignment(align)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                return it

            def _fmt(v, decimals=6):
                return f"{v:.{decimals}f}" if v is not None else "—"

            r = tuple(r)
            self._tbl.setItem(row, C.RUN_ID,  _it(r[0]))
            self._tbl.setItem(row, C.AID,     _it(r[1]))
            self._tbl.setItem(row, C.LABID,   _it(r[2], Qt.AlignLeft))
            self._tbl.setItem(row, C.TEMP,    _it(_fmt(r[3], 2)))
            self._tbl.setItem(row, C.PRESS,   _it(_fmt(r[4], 2)))
            self._tbl.setItem(row, C.SAL,     _it(_fmt(r[5], 2)))

            # per-gas CF + lock checkbox pairs
            for i, (cf_idx, lock_idx) in enumerate(zip(C.CF_COLS, C.LOCK_COLS)):
                cf_val   = r[6 + i * 2]
                lock_val = r[7 + i * 2]
                self._tbl.setItem(row, cf_idx, _it(_fmt(cf_val)))
                lock_it = QTableWidgetItem()
                lock_it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                lock_it.setCheckState(Qt.Checked if lock_val == 1 else Qt.Unchecked)
                lock_it.setData(Qt.UserRole, r[0])   # eqw_run_id
                lock_it.setData(Qt.UserRole + 1, GASES[i].lower())  # gas name
                self._tbl.setItem(row, lock_idx, lock_it)

            created = r[16]
            self._tbl.setItem(row, C.CREATED, _it(
                created.strftime("%Y-%m-%d %H:%M") if created else ""
            ))

            self._colour_row(row)

        self._tbl.blockSignals(False)

    def _colour_row(self, row: int):
        """Colour row based on whether any gas is locked."""
        any_locked = any(
            self._tbl.item(row, lc) is not None
            and self._tbl.item(row, lc).checkState() == Qt.Checked
            for lc in C.LOCK_COLS
        )
        colour = _LOCK_COL if any_locked else _GOOD_COL
        for c in range(self._tbl.columnCount()):
            it = self._tbl.item(row, c)
            if it:
                it.setBackground(QBrush(colour))

    # ── history table ─────────────────────────────────────────────────────────

    def _populate_history(self, hist):
        self._tbl_hist.setRowCount(0)
        for r in hist:
            row = self._tbl_hist.rowCount()
            self._tbl_hist.insertRow(row)
            r = tuple(r)
            vals = [
                str(r[0]),                                     # ID
                str(r[1]),                                     # Serial
                str(r[2] or ""),                               # n EQW
                f"{r[4]:.4f}" if r[4] else "—",               # He
                f"{r[5]:.4f}" if r[5] else "—",               # Ne
                f"{r[6]:.4f}" if r[6] else "—",               # Ar
                f"{r[7]:.4f}" if r[7] else "—",               # Kr
                f"{r[8]:.4f}" if r[8] else "—",               # Xe
            ]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                if r[3]:   # is_current
                    it.setBackground(QBrush(QColor(200, 230, 200)))
                self._tbl_hist.setItem(row, c, it)

    # ── live summary recompute ─────────────────────────────────────────────────

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() not in C.LOCK_COLS:
            return
        row = item.row()
        run_id = item.data(Qt.UserRole)
        gas    = item.data(Qt.UserRole + 1)
        new_lock = 1 if item.checkState() == Qt.Checked else 0

        # Update DB
        col = f"lock_{gas}"
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text(
                    f"UPDATE ngam.ng_eqw_run SET {col} = :v WHERE eqw_run_id = :rid"
                ), {"v": new_lock, "rid": run_id})
                conn.commit()
        except Exception as exc:
            log.error("Lock update failed: %s", exc)

        self._colour_row(row)
        self._recompute_summary()

    def _recompute_summary(self):
        """Rebuild aggregate from current lock states in table (not DB re-query)."""
        rows_data = []
        for tbl_row in range(self._tbl.rowCount()):
            d = {}
            for i, g in enumerate(GASES):
                g_lo = g.lower()
                cf_it = self._tbl.item(tbl_row, C.CF_COLS[i])
                lk_it = self._tbl.item(tbl_row, C.LOCK_COLS[i])
                cf_val = None
                if cf_it and cf_it.text() not in ("—", ""):
                    try:
                        cf_val = float(cf_it.text())
                    except ValueError:
                        pass
                lock_val = 1 if (lk_it and lk_it.checkState() == Qt.Checked) else 0
                d[f"cf_{g_lo}"]   = cf_val
                d[f"lock_{g_lo}"] = lock_val
            rows_data.append(d)

        agg = aggregate_cfs(rows_data)
        self._summary.update_computed(agg)
        self._last_agg = agg

    # ── create template ───────────────────────────────────────────────────────

    def _create_template(self):
        if not self._has_admin:
            show_message(self, "Permission Denied",
                         "Admin privilege required to create CF templates.")
            return

        agg = getattr(self, "_last_agg", {})
        if not any(agg.get(g) for g in GASES):
            show_message(self, "No Data",
                         "No unlocked EQW runs with valid CFs — nothing to create.")
            return

        ctype_id = self._cmb_ctype.currentData()
        equip_id = self._cmb_equip.currentData()
        user     = normalize_login_name(get_current_user_id())
        now      = datetime.now()

        # Collect unlocked run IDs
        unlocked_run_ids = []
        for tbl_row in range(self._tbl.rowCount()):
            any_unlocked = any(
                self._tbl.item(tbl_row, lc) is not None
                and self._tbl.item(tbl_row, lc).checkState() == Qt.Unchecked
                for lc in C.LOCK_COLS
            )
            if any_unlocked:
                id_it = self._tbl.item(tbl_row, C.RUN_ID)
                if id_it:
                    try:
                        unlocked_run_ids.append(int(id_it.text()))
                    except ValueError:
                        pass

        n_runs = len(unlocked_run_ids)

        def _cf(g):  return agg[g][0] if agg.get(g) else None
        def _err(g): return agg[g][1] if agg.get(g) else None

        try:
            with db_manager.get_connection() as conn:
                # Next serial number
                serial_row = conn.execute(text("""
                    SELECT COALESCE(MAX(serial_number), 0) + 1
                    FROM   ngam.ng_cf_template
                    WHERE  container_type_id = :ctype AND equipmentid = :equip
                """), {"ctype": ctype_id, "equip": equip_id}).fetchone()
                serial = int(serial_row[0])

                # Insert template (calc CFs only; applied = same as calc initially)
                tid_row = conn.execute(text("""
                    INSERT INTO ngam.ng_cf_template (
                        serial_number, container_type_id, equipmentid,
                        calc_cf_he, calc_err_he, calc_cf_ne, calc_err_ne,
                        calc_cf_ar, calc_err_ar, calc_cf_kr, calc_err_kr,
                        calc_cf_xe, calc_err_xe,
                        n_eqw_runs, is_current
                    ) VALUES (
                        :ser, :ctype, :equip,
                        :cf_he, :err_he, :cf_ne, :err_ne,
                        :cf_ar, :err_ar, :cf_kr, :err_kr,
                        :cf_xe, :err_xe,
                        :n, FALSE
                    ) RETURNING template_id
                """), {
                    "ser": serial, "ctype": ctype_id, "equip": equip_id,
                    "cf_he":  _cf("He"),  "err_he":  _err("He"),
                    "cf_ne":  _cf("Ne"),  "err_ne":  _err("Ne"),
                    "cf_ar":  _cf("Ar"),  "err_ar":  _err("Ar"),
                    "cf_kr":  _cf("Kr"),  "err_kr":  _err("Kr"),
                    "cf_xe":  _cf("Xe"),  "err_xe":  _err("Xe"),
                    "n": n_runs,
                })
                template_id = int(tid_row.fetchone()[0])

                # Junction rows
                for run_id in unlocked_run_ids:
                    conn.execute(text("""
                        INSERT INTO ngam.ng_cf_template_run (template_id, eqw_run_id)
                        VALUES (:tid, :rid)
                        ON CONFLICT DO NOTHING
                    """), {"tid": template_id, "rid": run_id})

                conn.commit()

        except Exception as exc:
            show_message(self, "Error", f"Create template failed:\n{exc}",
                         QMessageBox.Critical)
            return

        self._current_template_id = template_id
        self._btn_promote.setEnabled(self._has_admin)
        show_message(self, "Template Created",
                     f"Template #{template_id} (serial {serial}) created from "
                     f"{n_runs} EQW run(s).\nClick 'Promote to Applied' to activate.")
        self._load_runs()   # refresh history

    # ── promote ────────────────────────────────────────────────────────────────

    def _promote(self):
        if not self._has_admin:
            show_message(self, "Permission Denied",
                         "Admin privilege required to promote CFs.")
            return
        if self._current_template_id is None:
            show_message(self, "No Template", "Create a template first.")
            return

        agg = getattr(self, "_last_agg", {})
        dlg = _PromoteDialog(agg, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return

        notes  = dlg.notes()
        user   = normalize_login_name(get_current_user_id())
        now    = datetime.now()
        tid    = self._current_template_id
        ctype_id = self._cmb_ctype.currentData()
        equip_id = self._cmb_equip.currentData()

        def _cf(g):  return agg[g][0] if agg.get(g) else None
        def _err(g): return agg[g][1] if agg.get(g) else None

        try:
            with db_manager.get_connection() as conn:
                # Clear previous current
                conn.execute(text("""
                    UPDATE ngam.ng_cf_template
                    SET is_current = FALSE
                    WHERE container_type_id = :ctype
                      AND equipmentid       = :equip
                      AND is_current        = TRUE
                """), {"ctype": ctype_id, "equip": equip_id})

                # Promote
                conn.execute(text("""
                    UPDATE ngam.ng_cf_template
                    SET is_current     = TRUE,
                        applied_cf_he  = :cf_he,  applied_err_he = :err_he,
                        applied_cf_ne  = :cf_ne,  applied_err_ne = :err_ne,
                        applied_cf_ar  = :cf_ar,  applied_err_ar = :err_ar,
                        applied_cf_kr  = :cf_kr,  applied_err_kr = :err_kr,
                        applied_cf_xe  = :cf_xe,  applied_err_xe = :err_xe,
                        promoted_by    = :usr,
                        promoted_at    = :now,
                        notes          = :notes
                    WHERE template_id = :tid
                """), {
                    "cf_he":  _cf("He"),  "err_he":  _err("He"),
                    "cf_ne":  _cf("Ne"),  "err_ne":  _err("Ne"),
                    "cf_ar":  _cf("Ar"),  "err_ar":  _err("Ar"),
                    "cf_kr":  _cf("Kr"),  "err_kr":  _err("Kr"),
                    "cf_xe":  _cf("Xe"),  "err_xe":  _err("Xe"),
                    "usr": user, "now": now, "notes": notes or None,
                    "tid": tid,
                })
                conn.commit()

        except Exception as exc:
            show_message(self, "Error", f"Promote failed:\n{exc}", QMessageBox.Critical)
            return

        self._btn_promote.setEnabled(False)
        self._current_template_id = None
        show_message(self, "Promoted",
                     "CFs promoted to Applied. Future data reduction will use these values.")
        self._load_runs()


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dialog wrapper
# ─────────────────────────────────────────────────────────────────────────────

class EqwCFDialog(QDialog):
    """Top-level dialog wrapping EqwCFWidget."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NGAM – EQW Correction Factor Management")
        self.setWindowFlags(
            Qt.Window | Qt.WindowMinimizeButtonHint |
            Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint
        )
        self.setMinimumSize(1100, 600)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(EqwCFWidget(parent=self))

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone entry point (for testing)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    dlg = EqwCFDialog()
    dlg.show()
    sys.exit(app.exec_())
