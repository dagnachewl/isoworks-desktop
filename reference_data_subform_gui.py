"""
reference_data_subform_gui.py — Reference material data sub-form for IsoWorks.
Provides ReferenceDataSubform, an embeddable QWidget that displays and edits
the isotopic reference values associated with a selected reference material.

Each row in the table has inline Edit and Delete buttons.
The toolbar has an Add Value button to create new entries.
"""
from __future__ import annotations
import logging
from datetime import datetime
import getpass

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QTableView, QHeaderView, QMessageBox, QDialog, QDialogButtonBox,
    QFormLayout, QLineEdit, QAbstractItemView, QComboBox
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5.QtCore import Qt

from db_core import db_manager
from sqlalchemy import text

# ── Column indices ─────────────────────────────────────────────────────────────
_C_ID    = 0   # ReferenceDataID — hidden; used for row identity
_C_MEAS  = 1   # Measurable / parameter label
_C_VAL   = 2   # Certified value
_C_UNC   = 3   # Uncertainty
_C_UNIT  = 4   # Unit short name
_C_RDATE = 5   # Reference date
_C_FROM  = 6   # Available from
_C_TO    = 7   # Available to
_C_SRC   = 8   # Source
_C_EDIT  = 9   # Edit button (setIndexWidget)
_C_DEL   = 10  # Delete button (setIndexWidget)
_N_COLS  = 11

_HDR = ["ID", "Measurable", "Value", "Uncertainty",
        "Unit", "Ref Date", "From", "To", "Source", "", ""]

_STYLE_EDIT = """
    QPushButton { background:#1976d2; color:white; border:none;
                  padding:2px 8px; border-radius:3px; font-size:11px; }
    QPushButton:hover    { background:#1565c0; }
    QPushButton:disabled { background:#90caf9; }
"""
_STYLE_DEL = """
    QPushButton { background:#c0392b; color:white; border:none;
                  padding:2px 6px; border-radius:3px; font-size:11px; }
    QPushButton:hover    { background:#a93226; }
    QPushButton:disabled { background:#f1948a; }
"""
_STYLE_ADD = """
    QPushButton { background:#27ae60; color:white; font-weight:bold;
                  border:none; padding:4px 14px; border-radius:4px; }
    QPushButton:hover    { background:#219a52; }
    QPushButton:disabled { background:#a9dfbf; color:#7f8c8d; }
"""


class ReferenceDataSubform(QWidget):
    """
    Embedded sub-panel showing ReferenceControlData rows for one ReferenceControl.
    Each row has inline Edit and Delete buttons; the toolbar has an Add Value button.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_reference_id = None

        l = QVBoxLayout(self)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(4)

        # ── Toolbar ────────────────────────────────────────────────────────
        bar = QHBoxLayout()
        bar.addStretch()
        self.btnAdd = QPushButton("Add Value")
        self.btnAdd.setStyleSheet(_STYLE_ADD)
        self.btnAdd.setEnabled(False)
        bar.addWidget(self.btnAdd)
        l.addLayout(bar)

        # ── Table ──────────────────────────────────────────────────────────
        self.tbl = QTableView()
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.verticalHeader().setDefaultSectionSize(30)
        self.tbl.horizontalHeader().setHighlightSections(False)

        self.model = QStandardItemModel(0, _N_COLS)
        self.model.setHorizontalHeaderLabels(_HDR)
        self.tbl.setModel(self.model)
        l.addWidget(self.tbl, 1)

        # ── Signals ────────────────────────────────────────────────────────
        self.btnAdd.clicked.connect(self.on_add)
        self.tbl.doubleClicked.connect(
            lambda idx: self._open_edit(self._did_at(idx.row()))
        )

    # ── Public ────────────────────────────────────────────────────────────────

    def load_data(self, rid):
        self.current_reference_id = rid
        self.model.removeRows(0, self.model.rowCount())
        self.btnAdd.setEnabled(rid is not None)

        if not rid:
            return

        try:
            with db_manager.get_connection() as conn:
                sql = f"""
                    SELECT rd.ReferenceDataID,
                           m.ParameterLabel,
                           rd.CertifiedValue,
                           rd.CertifiedValueUnc,
                           u.ShortName,
                           rd.ReferenceDate,
                           rd.AvailableDateFrom,
                           rd.AvailableDateTo,
                           rd.Source
                    FROM   ReferenceControlData rd
                    LEFT JOIN Analytes m          ON rd.MeasurableID = m.AnalyteID
                    LEFT JOIN MeasurementUnit u   ON rd.UnitID       = u.UnitID
                    WHERE  rd.ReferenceID = :rid
                    ORDER  BY m.ParameterLabel, rd.AvailableDateFrom DESC
                """
                rows = conn.execute(text(sql), {"rid": rid}).fetchall()
        except Exception as exc:
            logging.error("ReferenceDataSubform.load_data: %s", exc)
            return

        for row_idx, r in enumerate(rows):
            did = r[0]

            # Model items — ID hidden, button columns are widget placeholders
            items = [QStandardItem(str(did))]           # _C_ID
            items.append(QStandardItem(r[1] or ""))     # _C_MEAS
            items.append(QStandardItem(
                str(r[2]) if r[2] is not None else "")) # _C_VAL
            items.append(QStandardItem(
                str(r[3]) if r[3] is not None else "")) # _C_UNC
            items.append(QStandardItem(r[4] or ""))     # _C_UNIT
            items.append(QStandardItem(
                str(r[5].date()) if r[5] else ""))      # _C_RDATE
            items.append(QStandardItem(
                str(r[6].date()) if r[6] else ""))      # _C_FROM
            items.append(QStandardItem(
                str(r[7].date()) if r[7] else ""))      # _C_TO
            items.append(QStandardItem(r[8] or ""))     # _C_SRC
            items.append(QStandardItem())               # _C_EDIT  (widget)
            items.append(QStandardItem())               # _C_DEL   (widget)
            self.model.appendRow(items)

            # Inline Edit button
            btn_edit = QPushButton("Edit")
            btn_edit.setFixedWidth(50)
            btn_edit.setStyleSheet(_STYLE_EDIT)
            btn_edit.clicked.connect(
                lambda checked, d=did: self._open_edit(d)
            )
            self.tbl.setIndexWidget(
                self.model.index(row_idx, _C_EDIT), btn_edit
            )

            # Inline Delete button
            btn_del = QPushButton("✕")
            btn_del.setFixedWidth(32)
            btn_del.setStyleSheet(_STYLE_DEL)
            btn_del.clicked.connect(
                lambda checked, d=did: self._on_delete(d)
            )
            self.tbl.setIndexWidget(
                self.model.index(row_idx, _C_DEL), btn_del
            )

        self._resize_columns()

    # ── Slots ─────────────────────────────────────────────────────────────────

    def on_add(self):
        dlg = EditCertifiedValueDialog(self.current_reference_id, None, self)
        if dlg.exec_() == QDialog.Accepted:
            self.load_data(self.current_reference_id)

    def _open_edit(self, did):
        if did is None:
            return
        dlg = EditCertifiedValueDialog(self.current_reference_id, did, self)
        if dlg.exec_() == QDialog.Accepted:
            self.load_data(self.current_reference_id)

    def _on_delete(self, did):
        if QMessageBox.question(
            self, "Delete Value",
            "Permanently delete this certified value?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ) == QMessageBox.No:
            return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(
                    text("DELETE FROM ReferenceControlData WHERE ReferenceDataID=:did"),
                    {"did": did}
                )
                conn.commit()
            self.load_data(self.current_reference_id)
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _did_at(self, row: int):
        """Return the ReferenceDataID stored in the hidden ID column for *row*."""
        item = self.model.item(row, _C_ID)
        if item is None:
            return None
        try:
            return int(item.text())
        except ValueError:
            return None

    def _resize_columns(self):
        self.tbl.setColumnHidden(_C_ID, True)
        self.tbl.setColumnWidth(_C_EDIT, 54)
        self.tbl.setColumnWidth(_C_DEL,  36)
        self.tbl.resizeColumnsToContents()
        # Keep button columns fixed after resizeColumnsToContents
        self.tbl.setColumnWidth(_C_EDIT, 54)
        self.tbl.setColumnWidth(_C_DEL,  36)
        self.tbl.horizontalHeader().setSectionResizeMode(
            _C_MEAS, QHeaderView.Stretch
        )


# ── Value editor dialog ────────────────────────────────────────────────────────

class EditCertifiedValueDialog(QDialog):
    def __init__(self, rid, did=None, parent=None):
        super().__init__(parent)
        self.rid    = rid
        self.did    = did
        self.is_new = (did is None)

        self.setWindowTitle("New Value" if self.is_new else "Edit Value")
        self.setModal(True)
        self.setMinimumWidth(420)

        l = QVBoxLayout(self)
        f = QFormLayout()

        self.cmbMeas  = QComboBox()
        self.txtVal   = QLineEdit()
        self.txtUnc   = QLineEdit()
        self.cmbUnit  = QComboBox()
        self.dteRef   = QLineEdit(); self.dteRef.setPlaceholderText("YYYY-MM-DD")
        self.dteFrom  = QLineEdit(); self.dteFrom.setPlaceholderText("YYYY-MM-DD")
        self.dteTo    = QLineEdit(); self.dteTo.setPlaceholderText("YYYY-MM-DD")
        self.txtSrc   = QLineEdit()

        f.addRow("Measurable:",  self.cmbMeas)
        f.addRow("Value:",       self.txtVal)
        f.addRow("Uncertainty:", self.txtUnc)
        f.addRow("Unit:",        self.cmbUnit)
        f.addRow("Ref Date:",    self.dteRef)
        f.addRow("From:",        self.dteFrom)
        f.addRow("To:",          self.dteTo)
        f.addRow("Source:",      self.txtSrc)
        l.addLayout(f)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        l.addWidget(bb)

        self._load_combos()
        self._load_data()

    def _load_combos(self):
        try:
            with db_manager.get_connection() as conn:
                for r in conn.execute(text(
                    "SELECT AnalyteID, ParameterLabel "
                    "FROM Analytes ORDER BY AnalyteID"
                )):
                    self.cmbMeas.addItem(r[1], r[0])

                self.cmbUnit.addItem("", None)
                for r in conn.execute(text(
                    f"SELECT UnitID, ShortName FROM MeasurementUnit "
                    f"WHERE IsDefault={db_manager.sql_bool(True)} ORDER BY UnitID"
                )):
                    self.cmbUnit.addItem(r[1], r[0])
        except Exception as exc:
            logging.error("EditCertifiedValueDialog._load_combos: %s", exc)

    def _load_data(self):
        if self.is_new:
            self.dteFrom.setText(datetime.now().date().isoformat())
            return
        try:
            with db_manager.get_connection() as conn:
                r = conn.execute(
                    text("SELECT * FROM ReferenceControlData "
                         "WHERE ReferenceDataID=:did"),
                    {"did": self.did}
                ).fetchone()
                if r:
                    self.cmbMeas.setCurrentIndex(
                        self.cmbMeas.findData(r.MeasurableID))
                    self.txtVal.setText(
                        str(r.CertifiedValue) if r.CertifiedValue is not None else "")
                    self.txtUnc.setText(
                        str(r.CertifiedValueUnc) if r.CertifiedValueUnc is not None else "")
                    self.cmbUnit.setCurrentIndex(self.cmbUnit.findData(r.UnitID))
                    self.dteRef.setText(
                        str(r.ReferenceDate.date()) if r.ReferenceDate else "")
                    self.dteFrom.setText(
                        str(r.AvailableDateFrom.date()) if r.AvailableDateFrom else "")
                    self.dteTo.setText(
                        str(r.AvailableDateTo.date()) if r.AvailableDateTo else "")
                    self.txtSrc.setText(r.Source or "")
        except Exception as exc:
            logging.error("EditCertifiedValueDialog._load_data: %s", exc)

    def save(self):
        if not self.txtVal.text():
            QMessageBox.warning(self, "Validation", "Value is required.")
            return
        try:
            with db_manager.get_connection() as conn:
                p = {
                    "rid": self.rid,
                    "mid": self.cmbMeas.currentData(),
                    "val": float(self.txtVal.text()),
                    "unc": float(self.txtUnc.text()) if self.txtUnc.text() else None,
                    "uid": self.cmbUnit.currentData(),
                    "ref": self.dteRef.text() or None,
                    "fr":  self.dteFrom.text() or None,
                    "to":  self.dteTo.text() or None,
                    "src": self.txtSrc.text() or None,
                }
                if self.is_new:
                    conn.execute(text("""
                        INSERT INTO ReferenceControlData
                            (ReferenceID, MeasurableID, CertifiedValue,
                             CertifiedValueUnc, UnitID, ReferenceDate,
                             AvailableDateFrom, AvailableDateTo, Source)
                        VALUES
                            (:rid, :mid, :val, :unc, :uid, :ref, :fr, :to, :src)
                    """), p)
                else:
                    p["did"] = self.did
                    conn.execute(text("""
                        UPDATE ReferenceControlData
                        SET    MeasurableID=:mid, CertifiedValue=:val,
                               CertifiedValueUnc=:unc, UnitID=:uid,
                               ReferenceDate=:ref, AvailableDateFrom=:fr,
                               AvailableDateTo=:to, Source=:src
                        WHERE  ReferenceDataID=:did
                    """), p)
                conn.commit()
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))
