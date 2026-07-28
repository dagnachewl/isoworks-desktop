"""
ams_run_details_gui.py — AMS 14C Run Details window for IsoWorks.

Displays run metadata, imported wheels, per-target raw ratios, and
(after reduction) Fm / pMC / conventional age results.

Toolbar actions:
  Edit          — toggle metadata editing
  Import Wheel  — parse a Tandetron file and load it into the DB
  Reduce        — OXI normalisation + δ¹³C correction + blank subtraction
  Print         — load-list PDF
  Close

Entry point: AMSRunDetailsWindow(run_id, parent=None)
"""
from __future__ import annotations
import logging
import os
from datetime import datetime
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLineEdit, QComboBox, QTextEdit, QPushButton, QLabel,
    QTabWidget, QTableView, QHeaderView, QMessageBox,
    QFileDialog, QAbstractItemView, QDesktopWidget, QDateEdit
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QFont, QColor
from PyQt5.QtCore import Qt, QDate

from db_core import db_manager
from sqlalchemy import text
from shared_utils import get_current_user_id, check_employee_privilege, normalize_login_name
from ams_parser import HVEETandetronParser, import_wheel_to_db
from ams_reduction import reduce_run, WheelReductionResult

log = logging.getLogger(__name__)

# ── Style constants ────────────────────────────────────────────────────────────

_META_STYLE = """
QGroupBox {
    font-weight: bold; font-size: 12px;
    border: 1px solid #C8D0DC; border-radius: 6px;
    margin-top: 8px; padding: 6px 8px;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #2D4A8A; }
QLineEdit, QComboBox, QTextEdit, QDateEdit {
    background: #FFFFFF; border: 1px solid #D0D0DD;
    border-radius: 6px; padding: 4px 6px; font-size: 12.5px;
}
QLineEdit:read-only, QDateEdit:read-only { background: #F5F6FA; }
QLineEdit:focus, QComboBox:focus, QDateEdit:focus { border: 1px solid #4C82FF; }
QDateEdit::drop-down { background: #C5CAE9; border: none; width: 22px; border-radius: 0 6px 6px 0; }
QDateEdit::drop-down:hover { background: #7986CB; }
QDateEdit::drop-down:pressed { background: #3949AB; }
QDateEdit::down-arrow { image: none; width: 0; height: 0; border-left: 5px solid transparent; border-right: 5px solid transparent; border-top: 6px solid #3949AB; }
"""

_TABLE_STYLE = """
QTableView {
    border: 1px solid #E0E4EC; border-radius: 4px;
    background: #FFFFFF; alternate-background-color: #F5F7FC;
    gridline-color: #E8EAF0; font-size: 12px;
}
QTableView::item { padding: 4px 8px; }
QTableView::item:selected { background: #DDEEFF; color: #000; }
QHeaderView::section {
    background: #EEF1F7; color: #444; font-weight: bold;
    border: none; border-bottom: 2px solid #C8D0DC;
    padding: 5px 8px; font-size: 12px;
}
"""

_STATUS_LABELS = {0: "Open", 1: "Reduced", 2: "Approved", 3: "Locked"}

# Target-type row colours in the load list table
_TYPE_COLORS = {
    "OXI":            QColor("#E3F2FD"),
    "OXII":           QColor("#E8EAF6"),
    "process_blank":  QColor("#FFF3E0"),
    "graphite_blank": QColor("#FFF8E1"),
    "secondary_std":  QColor("#F3E5F5"),
}


class AMSRunDetailsWindow(QDialog):
    """AMS run viewer with Edit, Import Wheel, Reduce, Print and Close."""

    def __init__(self, run_id: int, parent=None):
        super().__init__(parent)
        self.run_id = run_id
        self._edit_mode = False
        self.current_user = get_current_user_id()
        self._has_write = False

        self.setWindowTitle(f"AMS Run {run_id}")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setStyleSheet(_META_STYLE)

        scr = QDesktopWidget().screenGeometry(0)
        w, h = 1300, 900
        self.setGeometry(
            scr.left() + (scr.width()  - w) // 2,
            scr.top()  + (scr.height() - h) // 2,
            w, h
        )

        try:
            db_manager.get_engine()
        except Exception:
            QMessageBox.critical(self, "Error", "Database connection is not set.")
            return

        self._check_privileges()

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        self._build_toolbar(layout)
        self._build_metadata(layout)
        self._build_tabs(layout)
        self._populate_combos()
        self._refresh_all()

    # ── Privileges ─────────────────────────────────────────────────────────────

    def _check_privileges(self):
        try:
            u = normalize_login_name(self.current_user)
            self._has_write = check_employee_privilege(u, "accessams")
        except Exception:
            self._has_write = False

    # ── Toolbar ────────────────────────────────────────────────────────────────

    def _build_toolbar(self, layout: QVBoxLayout):
        bar = QHBoxLayout()
        bar.addStretch()

        self.btnEdit = QPushButton("Edit")
        self.btnEdit.setCheckable(True)
        self.btnEdit.setEnabled(self._has_write)
        self.btnEdit.toggled.connect(self._toggle_edit)

        self.btnImport = QPushButton("Import Wheel File")
        self.btnImport.setStyleSheet("""
            QPushButton { background:#1976d2; color:white; font-weight:bold;
                          border:none; padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background:#1565c0; }
            QPushButton:disabled { background:#90caf9; }
        """)
        self.btnImport.setEnabled(self._has_write)
        self.btnImport.clicked.connect(self._import_wheel)

        self.btnReduce = QPushButton("Reduce")
        self.btnReduce.setToolTip(
            "OXI normalisation · δ¹³C fractionation correction · blank subtraction"
        )
        self.btnReduce.setStyleSheet("""
            QPushButton { background:#e67e22; color:white; font-weight:bold;
                          border:none; padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background:#d35400; }
            QPushButton:disabled { background:#f0c080; }
        """)
        self.btnReduce.setEnabled(self._has_write)
        self.btnReduce.clicked.connect(self._run_reduction)

        self.btnPrint = QPushButton("Print Load List")
        self.btnPrint.clicked.connect(self._print_loadlist)

        btnClose = QPushButton("Close")
        btnClose.clicked.connect(self.accept)

        for w in [self.btnEdit, self.btnImport, self.btnReduce,
                  self.btnPrint, btnClose]:
            bar.addWidget(w)

        layout.addLayout(bar)

    # ── Metadata panel ─────────────────────────────────────────────────────────

    def _build_metadata(self, layout: QVBoxLayout):
        grp = QGroupBox("Run Information")
        form = QFormLayout(grp)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(8)

        self.txtRunCode   = QLineEdit(); self.txtRunCode.setReadOnly(True)
        self.dtRunDate    = QDateEdit();  self.dtRunDate.setReadOnly(True)
        self.dtRunDate.setCalendarPopup(True)
        self.dtRunDate.setDisplayFormat("yyyy-MM-dd")
        self.txtEquipment = QLineEdit(); self.txtEquipment.setReadOnly(True)
        self.cmbStatus    = QComboBox();  self.cmbStatus.setEnabled(False)
        for code, label in _STATUS_LABELS.items():
            self.cmbStatus.addItem(label, code)
        self.cmbTechnician = QComboBox(); self.cmbTechnician.setEnabled(False)
        self.txtNotes      = QTextEdit(); self.txtNotes.setReadOnly(True)
        self.txtNotes.setFixedHeight(52)

        form.addRow("Run Code",   self.txtRunCode)
        form.addRow("Run Date",   self.dtRunDate)
        form.addRow("Equipment",  self.txtEquipment)
        form.addRow("Status",     self.cmbStatus)
        form.addRow("Technician", self.cmbTechnician)
        form.addRow("Notes",      self.txtNotes)

        # Save / Cancel buttons (hidden until edit mode)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btnSave   = QPushButton("Save Changes")
        self.btnCancel = QPushButton("Cancel")
        self.btnSave.setStyleSheet("""
            QPushButton { background:#27ae60; color:white; font-weight:bold;
                          border:none; padding:4px 14px; border-radius:4px; }
            QPushButton:hover { background:#219a52; }
        """)
        self.btnSave.clicked.connect(self._save_metadata)
        self.btnCancel.clicked.connect(lambda: self.btnEdit.setChecked(False))
        btn_row.addWidget(self.btnSave)
        btn_row.addWidget(self.btnCancel)
        self.btnSave.hide()
        self.btnCancel.hide()

        vlay = QVBoxLayout()
        vlay.addWidget(grp)
        vlay.addLayout(btn_row)
        layout.addLayout(vlay)

    # ── Tabs ───────────────────────────────────────────────────────────────────

    def _build_tabs(self, layout: QVBoxLayout):
        tabs = QTabWidget()

        # ── Tab 1: Targets (load list) ─────────────────────────────────────────
        self._targets_model = QStandardItemModel()
        self._targets_table = QTableView()
        self._targets_table.setModel(self._targets_model)
        self._targets_table.setStyleSheet(_TABLE_STYLE)
        self._targets_table.setAlternatingRowColors(True)
        self._targets_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._targets_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._targets_table.verticalHeader().setVisible(False)
        tabs.addTab(self._targets_table, "Targets / Load List")

        # ── Tab 2: Results (reduced) ────────────────────────────────────────────
        self._results_model = QStandardItemModel()
        self._results_table = QTableView()
        self._results_table.setModel(self._results_model)
        self._results_table.setStyleSheet(_TABLE_STYLE)
        self._results_table.setAlternatingRowColors(True)
        self._results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._results_table.verticalHeader().setVisible(False)
        tabs.addTab(self._results_table, "Results (Fm / pMC / Age)")

        layout.addWidget(tabs, 1)
        self._tabs = tabs

    # ── Combo population ───────────────────────────────────────────────────────

    def _populate_combos(self):
        try:
            with db_manager.get_connection() as conn:
                _en = db_manager.sql_concat('LastName', "', '", 'FirstMiddleName')
                rows = conn.execute(text(f"""
                    SELECT EmployeeID, {_en} AS FullName
                    FROM Employee
                    WHERE IsObsolete IS NOT TRUE
                    ORDER BY LastName
                """)).fetchall()
            self.cmbTechnician.addItem("— unassigned —", None)
            for r in rows:
                self.cmbTechnician.addItem(r.FullName, r.EmployeeID)
        except Exception as exc:
            log.warning("AMSRunDetailsWindow._populate_combos: %s", exc)

    # ── Edit mode toggle ───────────────────────────────────────────────────────

    def _toggle_edit(self, on: bool):
        self._edit_mode = on
        self.btnEdit.setText("Cancel Edit" if on else "Edit")

        ro = not on
        self.dtRunDate.setReadOnly(ro)
        self.cmbStatus.setEnabled(on)
        self.cmbTechnician.setEnabled(on)
        self.txtNotes.setReadOnly(ro)

        self.btnSave.setVisible(on)
        self.btnCancel.setVisible(on)

        if not on:
            self._refresh_metadata()

    # ── Data refresh ───────────────────────────────────────────────────────────

    def _refresh_all(self):
        self._refresh_metadata()
        self._refresh_targets()
        self._refresh_results()

    def _refresh_metadata(self):
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT r.runcode, r.rundate, r.runstatus,
                           r.technicianid, r.notes,
                           e.EquipmentName
                    FROM ams.amsrun r
                    LEFT JOIN Equipment e ON e.EquipmentID = r.equipmentid
                    WHERE r.amsrunid = :rid
                """), {"rid": self.run_id}).fetchone()
        except Exception as exc:
            log.error("AMSRunDetails metadata: %s", exc)
            return

        if not row:
            QMessageBox.critical(self, "Error", f"Run {self.run_id} not found.")
            self.close()
            return

        self.txtRunCode.setText(row.runcode or "")
        if row.rundate:
            self.dtRunDate.setDate(QDate(row.rundate.year,
                                         row.rundate.month,
                                         row.rundate.day))
        self.txtEquipment.setText(row.EquipmentName or "")

        idx = self.cmbStatus.findData(row.runstatus or 0)
        if idx >= 0:
            self.cmbStatus.setCurrentIndex(idx)

        idx = self.cmbTechnician.findData(row.technicianid)
        if idx >= 0:
            self.cmbTechnician.setCurrentIndex(idx)

        self.txtNotes.setPlainText(row.notes or "")
        self.setWindowTitle(f"AMS Run {self.run_id}  —  {row.runcode or ''}")

    def _refresh_targets(self):
        headers = [
            "Wheel", "Wheel Label", "Position", "Label",
            "Type", "Cycles", "Runtime (s)",
            "¹⁴C/¹²C", "±", "¹³C/¹²C", "±",
            "¹²C (µA)", "¹⁴C counts", "Analysis ID"
        ]
        self._targets_model.clear()
        self._targets_model.setHorizontalHeaderLabels(headers)

        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT w.wheelnumber, w.wheellabel,
                           t.wheelposition, t.targetlabel, t.targettype,
                           t.ncycles, t.runtime_s, t.analysisid,
                           res.rawratio14_12, res.rawratio14_12_err,
                           res.ratio13_12,    res.ratio13_12_err,
                           -- aggregate beam / counts from measurements if stored
                           AVG(m.current12c_ua) AS avg_i12c,
                           SUM(m.counts14c)     AS total_n14
                    FROM ams.amswheel  w
                    JOIN ams.amstarget t   ON t.amswheelid  = w.amswheelid
                    LEFT JOIN ams.amsresult      res ON res.amstargetid = t.amstargetid
                    LEFT JOIN ams.amsmeasurement m   ON m.amstargetid   = t.amstargetid
                                                     AND m.isrejected IS NOT TRUE
                    WHERE w.amsrunid = :rid
                    GROUP BY w.wheelnumber, w.wheellabel,
                             t.wheelposition, t.targetlabel, t.targettype,
                             t.ncycles, t.runtime_s, t.analysisid,
                             res.rawratio14_12, res.rawratio14_12_err,
                             res.ratio13_12, res.ratio13_12_err
                    ORDER BY w.wheelnumber, t.wheelposition
                """), {"rid": self.run_id}).fetchall()
        except Exception as exc:
            log.error("AMSRunDetails targets: %s", exc)
            return

        def _fmt(v, decimals=3, scale=1.0):
            if v is None:
                return ""
            return f"{v * scale:.{decimals}e}" if scale != 1.0 else f"{v:.{decimals}e}"

        for r in rows:
            color = _TYPE_COLORS.get(r.targettype)
            cells = [
                QStandardItem(str(r.wheelnumber)),
                QStandardItem(r.wheellabel or ""),
                QStandardItem(str(r.wheelposition)),
                QStandardItem(r.targetlabel or ""),
                QStandardItem(r.targettype or ""),
                QStandardItem(str(r.ncycles) if r.ncycles else ""),
                QStandardItem(f"{r.runtime_s:.1f}" if r.runtime_s else ""),
                QStandardItem(_fmt(r.rawratio14_12)),
                QStandardItem(_fmt(r.rawratio14_12_err)),
                QStandardItem(_fmt(r.ratio13_12)),
                QStandardItem(_fmt(r.ratio13_12_err)),
                QStandardItem(f"{r.avg_i12c:.3f}" if r.avg_i12c else ""),
                QStandardItem(str(int(r.total_n14)) if r.total_n14 else ""),
                QStandardItem(str(r.analysisid) if r.analysisid else ""),
            ]
            if color:
                for cell in cells:
                    cell.setBackground(color)
            self._targets_model.appendRow(cells)

        h = self._targets_table.horizontalHeader()
        for i in range(5):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        for i in range(5, len(headers)):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)

    def _refresh_results(self):
        headers = [
            "Wheel", "Pos", "Label", "Type",
            "δ¹³C (‰)", "Fm raw", "±",
            "Fm corr.", "±", "pMC", "Age (BP)", "±", "Accept"
        ]
        self._results_model.clear()
        self._results_model.setHorizontalHeaderLabels(headers)

        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT w.wheelnumber, t.wheelposition,
                           t.targetlabel, t.targettype,
                           res.delta13c_permil,
                           res.fm_raw,        res.fm_raw_err,
                           res.fm_corrected,  res.fm_corrected_err,
                           res.pmc,
                           res.congage_bp,    res.congage_err,
                           res.isaccepted
                    FROM ams.amswheel  w
                    JOIN ams.amstarget  t   ON t.amswheelid  = w.amswheelid
                    JOIN ams.amsresult  res ON res.amstargetid = t.amstargetid
                    WHERE w.amsrunid = :rid
                      AND (res.fm_corrected IS NOT NULL OR res.fm_raw IS NOT NULL)
                    ORDER BY w.wheelnumber, t.wheelposition
                """), {"rid": self.run_id}).fetchall()
        except Exception as exc:
            log.error("AMSRunDetails results: %s", exc)
            return

        def _f(v, d=4):
            return f"{v:.{d}f}" if v is not None else ""

        def _age(v):
            return f"{int(round(v))}" if v is not None else ""

        for r in rows:
            accepted = r.isaccepted
            color = None if accepted else QColor("#FFEBEE")
            cells = [
                QStandardItem(str(r.wheelnumber)),
                QStandardItem(str(r.wheelposition)),
                QStandardItem(r.targetlabel or ""),
                QStandardItem(r.targettype  or ""),
                QStandardItem(_f(r.delta13c_permil, 2)),
                QStandardItem(_f(r.fm_raw,       4)),
                QStandardItem(_f(r.fm_raw_err,   4)),
                QStandardItem(_f(r.fm_corrected, 4)),
                QStandardItem(_f(r.fm_corrected_err, 4)),
                QStandardItem(_f(r.pmc, 2)),
                QStandardItem(_age(r.congage_bp)),
                QStandardItem(_age(r.congage_err)),
                QStandardItem("✓" if accepted else "✗"),
            ]
            if color:
                for cell in cells:
                    cell.setBackground(color)
            self._results_model.appendRow(cells)

        h = self._results_table.horizontalHeader()
        for i in range(len(headers)):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)

    # ── Save metadata ──────────────────────────────────────────────────────────

    def _save_metadata(self):
        py_date  = self.dtRunDate.date().toPyDate()
        status   = self.cmbStatus.currentData()
        tech_id  = self.cmbTechnician.currentData()
        notes    = self.txtNotes.toPlainText().strip() or None
        now      = datetime.now()
        user     = self.current_user

        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE ams.amsrun
                    SET rundate        = :rdate,
                        runstatus      = :status,
                        technicianid   = :tid,
                        notes          = :notes,
                        modifdatestamp = :now,
                        modifuserstamp = :usr
                    WHERE amsrunid = :rid
                """), {
                    "rdate":  py_date,
                    "status": status,
                    "tid":    tech_id,
                    "notes":  notes,
                    "now":    now,
                    "usr":    user,
                    "rid":    self.run_id,
                })
                conn.commit()
            self.btnEdit.setChecked(False)
        except Exception as exc:
            log.error("AMSRunDetails save: %s", exc)
            QMessageBox.critical(self, "Save Error", str(exc))

    # ── Wheel import ───────────────────────────────────────────────────────────

    def _import_wheel(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select HVEE Tandetron Data File",
            "", "Data files (*.dat *.txt *.ams *.csv);;All files (*)"
        )
        if not path:
            return

        try:
            parser = HVEETandetronParser()
            wheel  = parser.parse(path)
        except Exception as exc:
            log.error("AMS parse error: %s", exc)
            QMessageBox.critical(self, "Parse Error",
                                 f"Could not parse file:\n{exc}")
            return

        # Determine next wheel number for this run
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT COALESCE(MAX(wheelnumber), 0) + 1
                    FROM ams.amswheel
                    WHERE amsrunid = :rid
                """), {"rid": self.run_id}).fetchone()
            wheel_number = row[0]
        except Exception as exc:
            log.error("AMS wheel number query: %s", exc)
            wheel_number = 1

        # Confirm with user
        n_targets  = len(wheel.targets)
        n_unknown  = sum(1 for t in wheel.targets if t.target_type == "unknown")
        n_std      = sum(1 for t in wheel.targets if t.target_type in ("OXI", "OXII"))
        n_blank    = sum(1 for t in wheel.targets if "blank" in t.target_type)

        msg = (
            f"<b>File:</b> {Path(path).name}<br>"
            f"<b>Wheel label:</b> {wheel.wheel_label}<br>"
            f"<b>Run code:</b> {wheel.run_code}<br>"
            f"<b>Date:</b> {wheel.run_date}<br><br>"
            f"<b>{n_targets} targets</b> — "
            f"{n_unknown} unknowns · {n_std} standards · {n_blank} blanks<br><br>"
            f"Import as Wheel <b>{wheel_number}</b> of Run <b>{self.run_id}</b>?"
        )
        if QMessageBox.question(
            self, "Confirm Import", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        ) == QMessageBox.No:
            return

        try:
            import_wheel_to_db(wheel, run_id=self.run_id, wheel_number=wheel_number)
        except Exception as exc:
            log.error("AMS import_wheel_to_db: %s", exc)
            QMessageBox.critical(self, "Import Error", str(exc))
            return

        QMessageBox.information(
            self, "Imported",
            f"Wheel {wheel_number} ({wheel.wheel_label}) imported successfully.\n"
            f"{n_targets} targets loaded."
        )
        self._refresh_targets()
        self._refresh_results()

    # ── Data reduction ─────────────────────────────────────────────────────────

    def _run_reduction(self):
        confirm = QMessageBox.question(
            self, "Run Reduction",
            f"<b>Reduce AMS Run {self.run_id}</b><br><br>"
            "This will:<br>"
            "• Normalise all targets to the OXI standard mean<br>"
            "• Apply δ¹³C fractionation correction<br>"
            "• Apply mass-balance blank subtraction where masses are set<br>"
            "• Overwrite any existing reduction results for this run<br><br>"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return

        self.btnReduce.setEnabled(False)
        self.btnReduce.setText("Reducing…")
        QApplication.processEvents()

        try:
            run_result = reduce_run(self.run_id, user=self.current_user)
        except Exception as exc:
            log.error("reduce_run %d: %s", self.run_id, exc, exc_info=True)
            QMessageBox.critical(self, "Reduction Error", str(exc))
            self.btnReduce.setEnabled(True)
            self.btnReduce.setText("Reduce")
            return
        finally:
            self.btnReduce.setEnabled(self._has_write)
            self.btnReduce.setText("Reduce")

        # ── Build summary report ───────────────────────────────────────────────
        lines: list[str] = []
        for wr in run_result.wheels:
            lines.append(
                f"<b>Wheel {wr.wheel_label}</b> — "
                f"{wr.n_unknown} unknowns · {wr.n_oxi} OXI · {wr.n_blank} blanks<br>"
                f"&nbsp;&nbsp;OXI mean R¹⁴/¹²: {wr.norm_r14:.4e} ± {wr.norm_r14_err:.2e}"
            )
            if wr.oxii_fm_check is not None:
                lines.append(
                    f"&nbsp;&nbsp;OXII check Fm: {wr.oxii_fm_check:.4f} "
                    f"(expected 1.3407)"
                )
            if wr.blank_fm is not None:
                lines.append(f"&nbsp;&nbsp;Process blank Fm: {wr.blank_fm:.4f}")
            for w in wr.warnings:
                lines.append(f"&nbsp;&nbsp;<font color='#e67e22'>⚠ {w}</font>")

        if run_result.errors:
            lines.append("<br><font color='#c0392b'><b>Errors:</b></font>")
            for e in run_result.errors:
                lines.append(f"&nbsp;&nbsp;{e}")

        total_targets = sum(len(wr.targets) for wr in run_result.wheels)
        summary = (
            f"<b>Reduction complete</b><br>"
            f"{len(run_result.wheels)} wheel(s) · {total_targets} targets reduced<br><br>"
            + "<br>".join(lines)
        )

        QMessageBox.information(self, "Reduction Summary", summary)
        self._refresh_results()
        self._tabs.setCurrentIndex(1)   # switch to Results tab

    # ── Print load list ────────────────────────────────────────────────────────

    def _print_loadlist(self):
        QMessageBox.information(
            self, "Print Load List",
            "Load-list printing for AMS will be available after the\n"
            "reduction module is implemented.\n\n"
            "Use Export (coming soon) to get a CSV of the target table."
        )
