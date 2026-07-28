"""
siam_preanalysis_details_gui.py — Pre-analysis batch details view for IsoWorks.
Provides a QWidget for viewing and editing the load list of an existing
pre-isotope analysis (chemistry) batch, including per-sample status and results.
"""
from __future__ import annotations
import logging
from datetime import datetime

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QComboBox, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QLabel, QMessageBox, QHeaderView, QAbstractItemView, QCheckBox
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QBrush, QFont

from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from shared_utils import check_employee_privilege


# ── Column indices for the load-list table ────────────────────────────────────
class _C:
    POS      = 0
    LAB_ID   = 1
    NAME     = 2
    STATUS   = 3    # editable combo/int  (1=Pending, 2=Measured, 3=Pass, 4=Fail, -9=Excluded)
    MEAS_VAL = 4    # editable float — measured result (e.g. µmol/L NO3)
    FIELD_VAL = 5   # threshold / expected value (read-only)
    PASS_REQ = 6    # boolean display
    REMARKS  = 7    # editable

_STATUS_OPTIONS = {
    1: "1 — Pending",
    2: "2 — Measured",
    3: "3 — Pass",
    4: "4 — Fail",
    -9: "-9 — Excluded",
}

_STATUS_COLORS = {
    3:  "#D1FAE5",   # green  — pass
    4:  "#FEE2E2",   # red    — fail
    2:  "#FEF9C3",   # yellow — measured, not yet evaluated
    1:  "",          # default
    -9: "#E5E7EB",   # grey   — excluded
}


class PreAnalysisDetailsWindow(QWidget):
    """
    View and edit a Pre-Isotope Analysis Batch (ChemistryBatch).

    Features
    --------
    * Read-only header (Run ID, procedure, instrument, start/end dates).
    * Editable load list: Status, Measured Value, Remarks.
    * 'Get Data' — pulls measured values from ChemistryData for a selected
      MeasurableID (if instrument data has already been imported).
    * 'Save Changes' — writes edits back to ChemistryLoadlist.
    * 'Finalize Batch' — stamps EndDate when all entries are evaluated.
    """

    def __init__(self, run_id: int, parent=None):
        super().__init__(parent)
        self.run_id = run_id
        self._edit_mode = False
        self._aid_map: dict[int, int] = {}   # row → AnalysisID

        self.setObjectName("PreAnalysisDetails")
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ── Header info ───────────────────────────────────────────────────
        hdr_grp = QGroupBox("Batch Header")
        hdr_lay = QFormLayout(hdr_grp)

        self.lblRunID      = QLineEdit(); self.lblRunID.setReadOnly(True)
        self.lblProcedure  = QLineEdit(); self.lblProcedure.setReadOnly(True)
        self.lblInstrument = QLineEdit(); self.lblInstrument.setReadOnly(True)
        self.lblStartDate  = QLineEdit(); self.lblStartDate.setReadOnly(True)
        self.lblEndDate    = QLineEdit(); self.lblEndDate.setReadOnly(True)
        self.cmbMeasurable = QComboBox()   # DefaultMeasurableID — for 'Get Data'
        self.lblThreshold  = QLabel("—")   # from GlobalParameters or procedure

        hdr_lay.addRow("Run ID:",     self.lblRunID)
        hdr_lay.addRow("Procedure:",  self.lblProcedure)
        hdr_lay.addRow("Instrument:", self.lblInstrument)
        hdr_lay.addRow("Start Date:", self.lblStartDate)
        hdr_lay.addRow("End Date:",   self.lblEndDate)
        hdr_lay.addRow("Parameter:",  self.cmbMeasurable)
        hdr_lay.addRow("Threshold:",  self.lblThreshold)
        root.addWidget(hdr_grp)

        # ── Toolbar ───────────────────────────────────────────────────────
        tb = QHBoxLayout()
        self.btnEdit     = QPushButton("Edit")
        self.btnSave     = QPushButton("Save Changes")
        self.btnGetData  = QPushButton("Get Data from Instrument")
        self.btnFinalize = QPushButton("Finalize Batch")
        self.btnFinalize.setStyleSheet("background-color:#2980b9;color:white;font-weight:bold;")
        self.btnSave.setEnabled(False)

        tb.addWidget(self.btnEdit)
        tb.addWidget(self.btnSave)
        tb.addSpacing(20)
        tb.addWidget(self.btnGetData)
        tb.addStretch(1)
        tb.addWidget(self.btnFinalize)
        root.addLayout(tb)

        # ── Load list table ───────────────────────────────────────────────
        cols = ["Pos", "OurLabID", "Sample Name", "Status", "Measured", "Threshold", "Pass?", "Remarks"]
        self.table = QTableWidget(0, len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeToContents)
        h.setSectionResizeMode(_C.NAME,    QHeaderView.Stretch)
        h.setSectionResizeMode(_C.REMARKS, QHeaderView.Stretch)
        root.addWidget(self.table, 1)

        # ── Status bar ────────────────────────────────────────────────────
        self.lblStat = QLabel("")
        root.addWidget(self.lblStat)

        # ── Signals ───────────────────────────────────────────────────────
        self.btnEdit.clicked.connect(self._toggle_edit)
        self.btnSave.clicked.connect(self._save_changes)
        self.btnGetData.clicked.connect(self._get_data_from_instrument)
        self.btnFinalize.clicked.connect(self._finalize_batch)

        # ── Privilege ─────────────────────────────────────────────────────
        has_write = check_employee_privilege("AccessSIAM")
        self.btnEdit.setEnabled(bool(has_write))
        self.btnFinalize.setEnabled(bool(has_write))

        self._load_header()
        self._load_loadlist()

    # ──────────────────────────────────────────────────────────────────────
    # Data loading
    # ──────────────────────────────────────────────────────────────────────

    def _load_header(self):
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT cb.runid, ap.procedurename,
                           COALESCE(eq.equipmentname, '—') AS instrument,
                           cb.runstarttime, cb.runendtime,
                           cb.defaultmeasurableid
                    FROM   chem.batch cb
                    JOIN   public.analysisprocedure ap ON ap.procedureid = cb.procedureid
                    LEFT JOIN public.equipment       eq ON eq.equipmentid = cb.equipmentid
                    WHERE  cb.runid = :rid
                """), {"rid": self.run_id}).fetchone()

                if not row:
                    self.lblStat.setText("Batch not found.")
                    return

                rid, proc, instr, start, end, default_meas = row
                self.lblRunID.setText(str(rid))
                self.lblProcedure.setText(proc or "")
                self.lblInstrument.setText(instr or "")
                self.lblStartDate.setText(
                    start.strftime("%Y-%m-%d %H:%M") if hasattr(start, "strftime") else str(start or ""))
                self.lblEndDate.setText(
                    end.strftime("%Y-%m-%d %H:%M") if hasattr(end, "strftime") else ("—" if not end else str(end)))

                # Populate measurable combo from analytes linked to the procedure
                self.cmbMeasurable.clear()
                measurables = conn.execute(text("""
                    SELECT a.analyteid, a.analytename
                    FROM public.analytes a
                    JOIN public.analysisprocedure_measurable apm ON apm.measurableid = a.analyteid
                    JOIN chem.batch cb ON cb.procedureid = apm.procedureid
                    WHERE cb.runid = :rid
                    ORDER BY a.analytename
                """), {"rid": self.run_id}).fetchall()
                for m in measurables:
                    self.cmbMeasurable.addItem(f"{m[0]} — {m[1]}", m[0])
                if default_meas:
                    for i in range(self.cmbMeasurable.count()):
                        if self.cmbMeasurable.itemData(i) == default_meas:
                            self.cmbMeasurable.setCurrentIndex(i)
                            break

                self.lblThreshold.setText("—")

        except Exception as exc:
            logging.error("PreAnalysisDetails._load_header: %s", exc)
            self.lblStat.setText(f"Error loading header: {exc}")

    def _load_loadlist(self):
        self.table.setRowCount(0)
        self._aid_map.clear()
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT cl.positionno,
                           a.prefix || '-' || CAST(a.sampleid AS TEXT) AS ourLabID,
                           s.sname,
                           cl.status,
                           cl.measuredvalue,
                           cl.fieldvalue,
                           cl.passrequirement,
                           cl.remarks,
                           cl.analysisid
                    FROM   chem.loadlist cl
                    JOIN   public.analysis a ON a.analysisid = cl.analysisid
                    LEFT JOIN public.sample s ON s.sampleid = a.sampleid AND s.prefix = a.prefix
                    WHERE  cl.runid = :rid
                    ORDER BY cl.positionno
                """), {"rid": self.run_id}).fetchall()

            for row_idx, row in enumerate(rows):
                pos, lab_id, name, status, meas_val, field_val, pass_req, remarks, aid = row
                self.table.insertRow(row_idx)
                self._aid_map[row_idx] = aid

                def _ro(val) -> QTableWidgetItem:
                    it = QTableWidgetItem(str(val) if val is not None else "")
                    it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    return it

                def _rw(val) -> QTableWidgetItem:
                    return QTableWidgetItem(str(val) if val is not None else "")

                self.table.setItem(row_idx, _C.POS,       _ro(pos))
                self.table.setItem(row_idx, _C.LAB_ID,    _ro(lab_id))
                self.table.setItem(row_idx, _C.NAME,      _ro(name or ""))
                self.table.setItem(row_idx, _C.STATUS,    _ro(_STATUS_OPTIONS.get(status, str(status or 1))))
                self.table.setItem(row_idx, _C.MEAS_VAL,  _rw(meas_val))
                self.table.setItem(row_idx, _C.FIELD_VAL, _ro(field_val))
                self.table.setItem(row_idx, _C.PASS_REQ,  _ro("✓" if pass_req else ""))
                self.table.setItem(row_idx, _C.REMARKS,   _rw(remarks or ""))

                # Row colour by status
                color = _STATUS_COLORS.get(status, "")
                if color:
                    brush = QBrush(QColor(color))
                    for c in range(self.table.columnCount()):
                        it = self.table.item(row_idx, c)
                        if it:
                            it.setBackground(brush)

            # Lock editable cells in read-only mode initially
            self._set_editable(False)
            n = self.table.rowCount()
            n_pass = sum(
                1 for r in range(n)
                if "3" in (self.table.item(r, _C.STATUS).text() or "")
            )
            self.lblStat.setText(f"{n} entries  |  {n_pass} passed")

        except Exception as exc:
            logging.error("PreAnalysisDetails._load_loadlist: %s", exc)
            self.lblStat.setText(f"Error loading load list: {exc}")

    # ──────────────────────────────────────────────────────────────────────
    # Edit / save
    # ──────────────────────────────────────────────────────────────────────

    def _toggle_edit(self):
        self._edit_mode = not self._edit_mode
        self._set_editable(self._edit_mode)
        self.btnEdit.setText("Stop Edit" if self._edit_mode else "Edit")
        self.btnSave.setEnabled(self._edit_mode)
        self.btnFinalize.setEnabled(not self._edit_mode and bool(check_employee_privilege("AccessSIAM")))

    def _set_editable(self, editable: bool):
        """Toggle editability of Status, Measured Value and Remarks columns."""
        editable_cols = (_C.STATUS, _C.MEAS_VAL, _C.REMARKS)
        for row in range(self.table.rowCount()):
            for col in editable_cols:
                it = self.table.item(row, col)
                if it:
                    if editable:
                        it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable)
                    else:
                        it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)

    def _save_changes(self):
        try:
            with db_manager.get_connection() as conn:
                for row in range(self.table.rowCount()):
                    aid = self._aid_map.get(row)
                    if aid is None:
                        continue

                    stat_text  = (self.table.item(row, _C.STATUS)   or QTableWidgetItem()).text().strip()
                    meas_text  = (self.table.item(row, _C.MEAS_VAL) or QTableWidgetItem()).text().strip()
                    rem_text   = (self.table.item(row, _C.REMARKS)   or QTableWidgetItem()).text().strip()

                    # Parse status code from "3 — Pass" style
                    stat_code = 1
                    for code, label in _STATUS_OPTIONS.items():
                        if str(code) in stat_text:
                            stat_code = code
                            break

                    meas_val = None
                    if meas_text:
                        try:
                            meas_val = float(meas_text)
                        except ValueError:
                            pass

                    # Auto pass/fail based on threshold if available
                    pass_req = None
                    try:
                        th_text = self.lblThreshold.text()
                        if th_text and th_text != "—" and meas_val is not None:
                            threshold = float(th_text)
                            pass_req = meas_val >= threshold
                            stat_code = 3 if pass_req else 4
                    except Exception as e:

                        logging.warning(f"Exception caught: {e}")

                    conn.execute(text("""
                        UPDATE chem.loadlist
                        SET    status          = :stat,
                               measuredvalue   = :meas,
                               passrequirement = :pass_req,
                               remarks         = :rem
                        WHERE  runid = :rid AND analysisid = :aid
                    """), {
                        "stat": stat_code,
                        "meas": meas_val,
                        "pass_req": pass_req,
                        "rem": rem_text or None,
                        "rid": self.run_id,
                        "aid": aid,
                    })

                conn.commit()

            show_message(self, "Saved", "Changes saved to ChemistryLoadlist.", QMessageBox.Information)
            self._toggle_edit()
            self._load_loadlist()

        except Exception as exc:
            logging.error("PreAnalysisDetails._save_changes: %s", exc)
            show_message(self, "Error", f"Save failed:\n{exc}", QMessageBox.Critical)

    # ──────────────────────────────────────────────────────────────────────
    # Get data from instrument table
    # ──────────────────────────────────────────────────────────────────────

    def _get_data_from_instrument(self):
        """
        Pull MeasuredValue from ChemistryData (ChemAnalysisID link) for the
        selected MeasurableID, and pre-fill the Measured column.
        """
        meas_id = self.cmbMeasurable.currentData()
        if not meas_id:
            show_message(self, "Select", "Please select a measurable parameter.", QMessageBox.Information)
            return

        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT cl.analysisid, cd.fvalue
                    FROM   chem.loadlist cl
                    JOIN   chem.data cd
                           ON  cd.chemanalysisid = cl.chemanalysisid
                           AND cd.measurableid   = :mid
                    WHERE  cl.runid = :rid
                """), {"rid": self.run_id, "mid": meas_id}).fetchall()

                aid_to_val = {r[0]: r[1] for r in rows}

            n_filled = 0
            for row in range(self.table.rowCount()):
                aid = self._aid_map.get(row)
                val = aid_to_val.get(aid)
                if val is not None:
                    it = self.table.item(row, _C.MEAS_VAL)
                    if it:
                        it.setText(str(val))
                        n_filled += 1

            if n_filled:
                show_message(self, "Done",
                             f"Filled {n_filled} measured value(s) from ChemistryData.\n"
                             "Click 'Save Changes' to persist.", QMessageBox.Information)
            else:
                show_message(self, "No Data",
                             "No matching records found in ChemistryData for this parameter.\n"
                             "Make sure data has been imported first.", QMessageBox.Warning)

        except Exception as exc:
            logging.error("PreAnalysisDetails._get_data: %s", exc)
            show_message(self, "Error", f"Could not retrieve data:\n{exc}", QMessageBox.Critical)

    # ──────────────────────────────────────────────────────────────────────
    # Finalize
    # ──────────────────────────────────────────────────────────────────────

    def _check_all_evaluated(self) -> bool:
        """Return True if every entry has Status ≥ 3 or == -9."""
        for row in range(self.table.rowCount()):
            stat_text = (self.table.item(row, _C.STATUS) or QTableWidgetItem()).text()
            code = None
            for c, lbl in _STATUS_OPTIONS.items():
                if str(c) in stat_text:
                    code = c; break
            if code is None or (code < 3 and code != -9):
                return False
        return True

    def _finalize_batch(self):
        if not self._check_all_evaluated():
            reply = QMessageBox.question(
                self, "Incomplete",
                "Some entries have not been evaluated yet (Status < 3).\n"
                "Finalize anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply == QMessageBox.No:
                return

        # Save default measurable
        meas_id = self.cmbMeasurable.currentData()
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE chem.batch
                    SET    runendtime         = :now,
                           defaultmeasurableid = :mid
                    WHERE  runid = :rid
                """), {"now": datetime.now(), "mid": meas_id, "rid": self.run_id})
                conn.commit()

            show_message(self, "Finalized",
                         f"Batch {self.run_id} finalized. End date stamped.",
                         QMessageBox.Information)
            self._load_header()
            self.btnFinalize.setEnabled(False)
            self.btnEdit.setEnabled(False)

        except Exception as exc:
            logging.error("PreAnalysisDetails._finalize_batch: %s", exc)
            show_message(self, "Error", f"Finalize failed:\n{exc}", QMessageBox.Critical)
