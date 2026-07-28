"""
ngam_ng_template_editor.py
===========================
Dialog to view and edit the Noble Gas sequence load-list template
stored in public.ngseqtemplate.

One row per inlet position in the run template, linked to a ProcedureID.

Table schema (public.ngseqtemplate):
    iinletid             – position index within the run (1-based)
    iport                – physical port number on the mass spec
    sampletype           – 0 = sample slot (filled at run creation),
                           nonzero = fixed reference (blank/standard/air)
    ourlabid             – lab ID of the fixed reference (NULL for sample slots)
    bisblank             – TRUE if this is a procedural blank
    bisreproreference    – TRUE if this is a reproducibility reference
    bislinreference      – TRUE if this is a linearity reference
    freferenceamount     – certified reference gas amount (ccSTP); supports
                           scientific notation values (e.g. 1.23e-13)
    freferenceamountunc  – uncertainty on freferenceamount (ccSTP)
    procedureid          – FK to public.analysisprocedure
"""
from __future__ import annotations

import logging
from typing import List, Optional

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView,
    QMessageBox, QCheckBox, QSpinBox,
    QComboBox, QDialogButtonBox, QWidget, QFrame,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QBrush

from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from sample_type_registry import MediaCode, STYPE_SAMPLE_SLOT

log = logging.getLogger(__name__)


class _NoScrollCombo(QComboBox):
    """QComboBox that ignores wheel events unless explicitly focused by a click."""
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


# ─── palette ──────────────────────────────────────────────────────────────────
_HDR_BG  = "#1F3A5F"
_TBLHDR  = (
    "QHeaderView::section { background-color:#3D6A9E; color:white; "
    "font-weight:bold; padding:3px 6px; border:none; "
    "border-right:1px solid #2E527A; }"
    "QHeaderView::section:last-section { border-right:none; }"
)

_COL_BLANK  = QColor(200, 230, 255)
_COL_REPRO  = QColor(200, 255, 230)
_COL_LIN    = QColor(255, 235, 190)
_COL_SAMPLE = QColor(255, 255, 255)
_COL_REF    = QColor(240, 230, 255)

# Column indices
class _C:
    POS       = 0
    PORT      = 1
    LABID     = 2
    BLANK     = 3
    REPRO     = 4
    LIN       = 5
    AMT       = 6
    AMT_UNC   = 7
    SAMP_TYPE = 8
    COUNT     = 9
    HEADERS = ["Inlet", "Port", "Lab ID / Reference",
               "Blank?", "Repro Ref?", "Lin Ref?",
               "Amount (ccSTP)", "Amt Unc (ccSTP)",
               "Sample Type"]

# Category → tblsampletype.mediacode for NGAM modules
_CAT_MEDIACODE = {11: MediaCode.INGROWTH, 14: MediaCode.NOBLE_GAS}


def _check_item(checked: bool) -> QTableWidgetItem:
    it = QTableWidgetItem()
    it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    it.setCheckState(Qt.Checked if checked else Qt.Unchecked)
    it.setTextAlignment(Qt.AlignCenter)
    return it


class NGSeqTemplateEditorDialog(QDialog):
    """
    Modal dialog for editing the load-list template for a Noble Gas MS
    Sequence procedure (public.ngseqtemplate).
    """

    def __init__(self, procedure_id: int, parent=None, run_only: bool = False,
                 preload_rows: Optional[List[dict]] = None):
        super().__init__(parent)
        self._proc_id = procedure_id
        self._run_only = run_only
        self._preload_rows = preload_rows   # if set, skip DB rows fetch
        self._proc_name = ""
        self._rows: List[dict] = []   # in-memory copy of the template
        self._dirty = False
        self._mediacode: Optional[int] = None
        self._sample_items: List[dict] = []  # cached {labid, sname, desc} for combo

        title = (f"Edit Load List — This Run Only  (Procedure {procedure_id})"
                 if run_only else
                 f"NG Sequence Template Editor — Procedure {procedure_id}")
        self.setWindowTitle(title)
        self.resize(880, 560)
        self.setMinimumSize(700, 400)

        self._build_ui()
        self._load()

    # ─── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # Header strip
        hdr = QLabel()
        hdr.setFixedHeight(28)
        hdr.setStyleSheet(
            f"background:{_HDR_BG}; color:white; font-weight:bold; font-size:12px;"
            "padding-left:8px;"
        )
        root.addWidget(hdr)
        self._hdr_lbl = hdr

        # Info row
        info = QHBoxLayout()
        info.addWidget(QLabel("Procedure:"))
        self.txtProc = QLineEdit()
        self.txtProc.setReadOnly(True)
        self.txtProc.setStyleSheet("background:#f5f5f5;")
        info.addWidget(self.txtProc, 1)
        info.addSpacing(20)
        info.addWidget(QLabel("Total positions:"))
        self.lblCount = QLabel("0")
        self.lblCount.setStyleSheet("font-weight:bold; min-width:36px;")
        info.addWidget(self.lblCount)
        root.addLayout(info)

        # Bulk amount row — QLineEdit accepts scientific notation (e.g. 1.23e-13)
        bulk = QHBoxLayout()
        bulk.addWidget(QLabel("Set Amount for all references:"))
        self.txtBulkAmount = QLineEdit()
        self.txtBulkAmount.setPlaceholderText("e.g. 1.23e-13")
        self.txtBulkAmount.setMaximumWidth(130)
        self.txtBulkAmount.setToolTip(
            "Apply this ccSTP amount to every non-empty reference position.\n"
            "Scientific notation accepted (e.g. 1.23e-13)."
        )
        bulk.addWidget(self.txtBulkAmount)
        bulk.addWidget(QLabel("± Unc:"))
        self.txtBulkAmountunc = QLineEdit()
        self.txtBulkAmountunc.setPlaceholderText("e.g. 1e-15")
        self.txtBulkAmountunc.setMaximumWidth(110)
        self.txtBulkAmountunc.setToolTip(
            "Uncertainty on the amount (ccSTP). Scientific notation accepted."
        )
        bulk.addWidget(self.txtBulkAmountunc)
        self.btnApplyAmount = QPushButton("Apply to Refs")
        self.btnApplyAmount.setFixedHeight(26)
        self.btnApplyAmount.setStyleSheet(
            "background:#3D6A9E; color:white; font-weight:bold; "
            "border:none; border-radius:3px; padding:2px 12px;"
        )
        self.btnApplyAmount.clicked.connect(self._apply_bulk_amount)
        bulk.addWidget(self.btnApplyAmount)
        bulk.addStretch()
        root.addLayout(bulk)

        # Table
        self.tbl = QTableWidget(0, _C.COUNT)
        self.tbl.setHorizontalHeaderLabels(_C.HEADERS)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setAlternatingRowColors(False)
        self.tbl.setStyleSheet(_TBLHDR)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setShowGrid(True)
        hh = self.tbl.horizontalHeader()
        for i in range(_C.COUNT - 1):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        # hh.setSectionResizeMode(_C.COUNT - 1, QHeaderView.Stretch)
        self.tbl.itemChanged.connect(self._on_cell_changed)
        root.addWidget(self.tbl, 1)

        # Action buttons
        btn_row = QHBoxLayout()
        self.btnAddRow   = QPushButton("Add Row")
        self.btnDelRow   = QPushButton("Remove Selected")
        self.btnMoveUp   = QPushButton("▲")
        self.btnMoveDown = QPushButton("▼")

        for b in (self.btnAddRow, self.btnDelRow, self.btnMoveUp, self.btnMoveDown):
            b.setFixedHeight(28)
        self.btnMoveUp.setFixedWidth(36)
        self.btnMoveDown.setFixedWidth(36)
        self.btnDelRow.setStyleSheet(
            "background:#c0392b; color:white; border:none; border-radius:3px; padding:2px 8px;"
        )

        self.btnAddRow.clicked.connect(self._add_row)
        self.btnDelRow.clicked.connect(self._del_row)
        self.btnMoveUp.clicked.connect(self._move_up)
        self.btnMoveDown.clicked.connect(self._move_down)

        btn_row.addWidget(self.btnAddRow)
        btn_row.addWidget(self.btnDelRow)
        btn_row.addStretch()
        btn_row.addWidget(self.btnMoveUp)
        btn_row.addWidget(self.btnMoveDown)
        root.addLayout(btn_row)

        # Dialog buttons
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#ccc;")
        root.addWidget(sep)

        if self._run_only:
            banner = QLabel(
                "⚠  Run-only mode — changes apply to this run only. "
                "The saved template will NOT be modified."
            )
            banner.setStyleSheet(
                "background:#FFF3CD; color:#856404; font-weight:bold; "
                "padding:4px 8px; border:1px solid #FFEEBA; border-radius:3px;"
            )
            banner.setWordWrap(True)
            root.addWidget(banner)

        dlg_btns = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        if self._run_only:
            btn = dlg_btns.button(QDialogButtonBox.Save)
            if btn:
                btn.setText("Apply to this run")
        dlg_btns.accepted.connect(self._save)
        dlg_btns.rejected.connect(self.reject)
        root.addWidget(dlg_btns)

    # ─── data loading ─────────────────────────────────────────────────────────

    def _load_sample_items(self):
        """Populate self._sample_items from sample + tblsampletype filtered by mediacode."""
        self._sample_items.clear()
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT
                        s.prefix || '-' || CAST(s.sampleid AS VARCHAR) AS labid,
                        COALESCE(s.sname, ''),
                        COALESCE(st.strshortdescription, '')
                    FROM public.sample s
                    JOIN public.tblsampletype st
                        ON st.intsampletype = s.sampletype
                    WHERE (:mc IS NULL OR st.mediacode = :mc)
                    ORDER BY s.prefix, s.sampleid
                """), {"mc": self._mediacode}).fetchall()
            for r in rows:
                self._sample_items.append({
                    "labid": r[0] or "",
                    "sname": r[1] or "",
                    "desc":  r[2] or "",
                })
        except Exception as e:
            log.error(f"Sample items load failed: {e}")

    def _load(self):
        try:
            with db_manager.get_connection() as conn:
                prow = conn.execute(text(
                    "SELECT procedurename, categoryid FROM public.analysisprocedure "
                    "WHERE procedureid = :pid"
                ), {"pid": self._proc_id}).fetchone()
                self._proc_name = prow[0] if prow else f"ID {self._proc_id}"
                cat_id = int(prow[1]) if prow and prow[1] is not None else 0
                self._mediacode = _CAT_MEDIACODE.get(cat_id)
                self._load_sample_items()

                if self._preload_rows is None:
                    rows = conn.execute(text("""
                        SELECT
                            t.iinletid, t.iport, t.sampletype, t.ourlabid,
                            COALESCE(t.bisblank, FALSE),
                            COALESCE(t.bisreproreference, FALSE),
                            COALESCE(t.bislinreference, FALSE),
                            t.freferenceamount,
                            t.freferenceamountunc,
                            COALESCE(st.strshortdescription, '') AS sample_type_desc
                        FROM public.ngseqtemplate t
                        LEFT JOIN public.sample s
                            ON s.prefix = split_part(t.ourlabid, '-', 1)
                            AND s.sampleid::text = split_part(t.ourlabid, '-', 2)
                        LEFT JOIN public.tblsampletype st
                            ON st.intsampletype = s.sampletype
                            AND (:mc IS NULL OR st.mediacode = :mc)
                        WHERE t.procedureid = :pid
                        ORDER BY t.iinletid
                    """), {"pid": self._proc_id, "mc": self._mediacode}).fetchall()

                    self._rows = []
                    for r in rows:
                        self._rows.append({
                            "iinletid":           r[0],
                            "iport":              r[1],
                            "sampletype":         int(r[2]) if r[2] is not None else 0,
                            "ourlabid":           r[3] or "",
                            "bisblank":           bool(r[4]),
                            "bisreproreference":  bool(r[5]),
                            "bislinreference":    bool(r[6]),
                            "freferenceamount":   r[7],
                            "freferenceamountunc": r[8],
                            "sample_type_desc":   r[9] or "",
                        })
                else:
                    # Caller supplied current in-memory rows — use them as-is
                    self._rows = list(self._preload_rows)

            self._proc_name = self._proc_name or f"Procedure {self._proc_id}"
            if self._run_only:
                self._hdr_lbl.setText(
                    f"  Run-only edit  —  {self._proc_name}  "
                    f"(saved template will NOT be changed)"
                )
            else:
                self._hdr_lbl.setText(
                    f"  Noble Gas Sequence Template  —  {self._proc_name}"
                )
            self.txtProc.setText(self._proc_name)

            self._repopulate_table()
            self._dirty = False

        except Exception as e:
            log.error(f"NGSeqTemplate load failed: {e}")
            show_message(self, "Load Error", str(e), QMessageBox.Critical)

    # ─── table painting ───────────────────────────────────────────────────────

    def _repopulate_table(self):
        self.tbl.blockSignals(True)
        self.tbl.setRowCount(0)
        for i, slot in enumerate(self._rows):
            self._insert_table_row(i, slot)
        self.tbl.blockSignals(False)
        self._renumber()
        self.lblCount.setText(str(len(self._rows)))

    def _insert_table_row(self, row_idx: int, slot: dict):
        self.tbl.insertRow(row_idx)
        stype = slot["sampletype"]

        # Colour
        if slot["bisblank"]:
            bg = _COL_BLANK
        elif slot["bislinreference"]:
            bg = _COL_LIN
        elif slot["bisreproreference"]:
            bg = _COL_REPRO
        elif stype != 0:
            bg = _COL_REF
        else:
            bg = _COL_SAMPLE

        def _item(val, editable=True, center=False):
            it = QTableWidgetItem(str(val) if val is not None else "")
            if not editable:
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            it.setBackground(QBrush(bg))
            if center:
                it.setTextAlignment(Qt.AlignCenter)
            return it

        # Pos (read-only, auto-numbered)
        self.tbl.setItem(row_idx, _C.POS, _item(row_idx + 1, editable=False, center=True))

        # Port
        self.tbl.setItem(row_idx, _C.PORT,
            _item(slot["iport"] if slot["iport"] is not None else "", center=True))

        # Lab ID / Reference — combo filtered by mediacode
        labid_combo = _NoScrollCombo()
        labid_combo.addItem("--", "")
        for si in self._sample_items:
            label = si["labid"]
            if si["sname"] or si["desc"]:
                extras = " — ".join(filter(None, [si["sname"], si["desc"]]))
                label = f"{si['labid']} — {extras}"
            labid_combo.addItem(label, si["labid"])
        idx = labid_combo.findData(slot["ourlabid"])
        if idx >= 0:
            labid_combo.setCurrentIndex(idx)
        labid_combo.setProperty("row", row_idx)
        labid_combo.currentIndexChanged.connect(self._on_labid_changed)
        labid_combo.setMaximumWidth(260)
        self.tbl.setCellWidget(row_idx, _C.LABID, labid_combo)

        # Checkboxes
        self.tbl.setItem(row_idx, _C.BLANK, _check_item(slot["bisblank"]))
        self.tbl.setItem(row_idx, _C.REPRO, _check_item(slot["bisreproreference"]))
        self.tbl.setItem(row_idx, _C.LIN,   _check_item(slot["bislinreference"]))

        # Ref Amount (ccSTP) — displayed in scientific notation for small values
        amt = slot["freferenceamount"]
        self.tbl.setItem(row_idx, _C.AMT,
            _item(f"{amt:.6g}" if amt is not None else "", center=True))

        # Ref Amount uncertainty
        unc = slot.get("freferenceamountunc")
        self.tbl.setItem(row_idx, _C.AMT_UNC,
            _item(f"{unc:.6g}" if unc is not None else "", center=True))

        # Sample Type (read-only, from sample table)
        self.tbl.setItem(row_idx, _C.SAMP_TYPE,
            _item(slot.get("sample_type_desc", ""), editable=False))

    # ─── signals ──────────────────────────────────────────────────────────────

    def _on_cell_changed(self, item: QTableWidgetItem):
        row = item.row()
        col = item.column()
        if row >= len(self._rows):
            return
        slot = self._rows[row]

        if col == _C.PORT:
            try:
                slot["iport"] = int(item.text()) if item.text().strip() else None
            except ValueError:
                slot["iport"] = None

        elif col == _C.BLANK:
            slot["bisblank"] = item.checkState() == Qt.Checked
            self._recolor_row(row)

        elif col == _C.REPRO:
            slot["bisreproreference"] = item.checkState() == Qt.Checked
            self._recolor_row(row)
            if slot["bisreproreference"]:
                self._validate_repro_reference(slot)

        elif col == _C.LIN:
            slot["bislinreference"] = item.checkState() == Qt.Checked
            self._recolor_row(row)

        elif col == _C.AMT:
            try:
                slot["freferenceamount"] = float(item.text()) if item.text().strip() else None
            except ValueError:
                slot["freferenceamount"] = None

        elif col == _C.AMT_UNC:
            try:
                slot["freferenceamountunc"] = float(item.text()) if item.text().strip() else None
            except ValueError:
                slot["freferenceamountunc"] = None

        self._dirty = True

    def _recolor_row(self, row: int):
        if row >= len(self._rows):
            return
        slot = self._rows[row]
        stype = slot["sampletype"]
        if slot["bisblank"]:
            bg = _COL_BLANK
        elif slot["bislinreference"]:
            bg = _COL_LIN
        elif slot["bisreproreference"]:
            bg = _COL_REPRO
        elif stype != 0:
            bg = _COL_REF
        else:
            bg = _COL_SAMPLE
        for col in (_C.POS, _C.PORT, _C.BLANK, _C.REPRO, _C.LIN, _C.AMT, _C.AMT_UNC, _C.SAMP_TYPE):
            it = self.tbl.item(row, col)
            if it:
                it.setBackground(QBrush(bg))

    def _on_labid_changed(self, _index: int):
        """Combo selection → update ourlabid, sampletype, and sample type display."""
        combo = self.sender()
        if combo is None:
            return
        row = combo.property("row")
        if row is None or row >= len(self._rows):
            return
        slot = self._rows[row]
        new_labid = combo.currentData() or ""
        slot["ourlabid"] = new_labid
        # Sample slots stay stype=0 regardless of labid; only reference slots toggle 4/0
        if slot.get("sampletype", 0) != STYPE_SAMPLE_SLOT:
            slot["sampletype"] = 4 if new_labid else STYPE_SAMPLE_SLOT
        # Update sample type description from cached items
        desc = ""
        if new_labid:
            for si in self._sample_items:
                if si["labid"] == new_labid:
                    desc = si["desc"]
                    break
        slot["sample_type_desc"] = desc
        it = self.tbl.item(row, _C.SAMP_TYPE)
        if it:
            it.setText(desc)
        self._recolor_row(row)
        self._dirty = True

    def _validate_repro_reference(self, slot: dict):
        """Check that a repro reference has certified values in ReferenceControlData."""
        labid = slot.get("ourlabid", "").strip()
        if not labid or "-" not in labid:
            show_message(self, "Invalid Reference",
                         f"Cannot validate '{labid}' — not a valid Lab ID.\n"
                         "Repro references must have certified values.",
                         QMessageBox.Warning)
            return

        pfx, sid_str = labid.split("-", 1)
        try:
            sid = int(sid_str)
        except ValueError:
            show_message(self, "Invalid Reference",
                         f"Cannot parse SampleID from '{labid}'.",
                         QMessageBox.Warning)
            return

        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT COUNT(*)
                    FROM public.referencecontrol rc
                    JOIN public.referencecontroldata rcd
                        ON rcd.referenceid = rc.referenceid
                    WHERE rc.prefix = :pfx
                      AND rc.sampleid = :sid
                      AND rcd.certifiedvalue IS NOT NULL
                """), {"pfx": pfx, "sid": sid}).fetchone()
                has_cert = row and int(row[0]) > 0
        except Exception as e:
            log.error(f"Repro reference validation failed: {e}")
            show_message(self, "Validation Error",
                         f"Could not verify certified values for '{labid}':\n{e}",
                         QMessageBox.Warning)
            return

        if not has_cert:
            show_message(self, "Missing Certified Values",
                         f"'{labid}' has no certified values in ReferenceControlData.\n"
                         "It cannot be used for calibration as a repro reference.\n\n"
                         "Please choose a different reference or add certified values.",
                         QMessageBox.Warning)

    def _validate_all_repro_references(self) -> List[str]:
        """Return a list of labids that lack certified values."""
        missing = []
        for slot in self._rows:
            if not slot.get("bisreproreference"):
                continue
            labid = slot.get("ourlabid", "").strip()
            if not labid or "-" not in labid:
                missing.append(labid or "(empty)")
                continue
            pfx, sid_str = labid.split("-", 1)
            try:
                sid = int(sid_str)
            except ValueError:
                missing.append(labid)
                continue
            try:
                with db_manager.get_connection() as conn:
                    row = conn.execute(text("""
                        SELECT COUNT(*)
                        FROM public.referencecontrol rc
                        JOIN public.referencecontroldata rcd
                            ON rcd.referenceid = rc.referenceid
                        WHERE rc.prefix = :pfx
                          AND rc.sampleid = :sid
                          AND rcd.certifiedvalue IS NOT NULL
                    """), {"pfx": pfx, "sid": sid}).fetchone()
                    if not row or int(row[0]) == 0:
                        missing.append(labid)
            except Exception:
                missing.append(labid)
        return missing

    # ─── bulk amount ──────────────────────────────────────────────────────────

    def _apply_bulk_amount(self):
        """Apply the bulk amount (and optional uncertainty) to all non-empty reference positions."""
        try:
            val = float(self.txtBulkAmount.text()) if self.txtBulkAmount.text().strip() else None
        except ValueError:
            show_message(self, "Invalid Amount",
                         "Enter a valid number (decimal or scientific notation, e.g. 1.23e-13).",
                         QMessageBox.Warning)
            return
        try:
            unc = float(self.txtBulkAmountunc.text()) if self.txtBulkAmountunc.text().strip() else None
        except ValueError:
            show_message(self, "Invalid Uncertainty",
                         "Enter a valid number for uncertainty (or leave blank).",
                         QMessageBox.Warning)
            return

        n = 0
        for slot in self._rows:
            if slot["ourlabid"]:
                if val is not None:
                    slot["freferenceamount"] = val
                if unc is not None:
                    slot["freferenceamountunc"] = unc
                n += 1
        if n:
            self._repopulate_table()
            self.lblCount.setText(str(len(self._rows)))
            self._dirty = True
        else:
            show_message(self, "No References",
                         "No reference positions found. Select a Lab ID first.",
                         QMessageBox.Information)

    # ─── row actions ──────────────────────────────────────────────────────────

    def _add_row(self):
        new_slot = {
            "iinletid":           len(self._rows) + 1,
            "iport":              None,
            "sampletype":         0,
            "ourlabid":           "",
            "bisblank":           False,
            "bisreproreference":  False,
            "bislinreference":    False,
            "freferenceamount":   None,
            "freferenceamountunc": None,
            "sample_type_desc":   "",
        }
        self._rows.append(new_slot)
        self.tbl.blockSignals(True)
        self._insert_table_row(len(self._rows) - 1, new_slot)
        self.tbl.blockSignals(False)
        self._renumber()
        self.lblCount.setText(str(len(self._rows)))
        self.tbl.scrollToBottom()
        self._dirty = True

    def _del_row(self):
        rows = sorted({idx.row() for idx in self.tbl.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for r in rows:
            self.tbl.removeRow(r)
            if r < len(self._rows):
                self._rows.pop(r)
        self._renumber()
        self.lblCount.setText(str(len(self._rows)))
        self._dirty = True

    def _move_up(self):
        idxs = sorted({i.row() for i in self.tbl.selectedIndexes()})
        if not idxs or idxs[0] == 0:
            return
        r = idxs[0]
        self._rows[r], self._rows[r - 1] = self._rows[r - 1], self._rows[r]
        self.tbl.blockSignals(True)
        self._repopulate_table()
        self.tbl.blockSignals(False)
        self.tbl.selectRow(r - 1)
        self._dirty = True

    def _move_down(self):
        idxs = sorted({i.row() for i in self.tbl.selectedIndexes()})
        if not idxs or idxs[-1] >= len(self._rows) - 1:
            return
        r = idxs[-1]
        self._rows[r], self._rows[r + 1] = self._rows[r + 1], self._rows[r]
        self.tbl.blockSignals(True)
        self._repopulate_table()
        self.tbl.blockSignals(False)
        self.tbl.selectRow(r + 1)
        self._dirty = True

    def _renumber(self):
        """Update the Inlet (Pos) column and internal iinletid after reorder."""
        for i, slot in enumerate(self._rows):
            slot["iinletid"] = i + 1
            it = self.tbl.item(i, _C.POS)
            if it:
                it.setText(str(i + 1))
        # Update row property on labid combos
        for i in range(self.tbl.rowCount()):
            w = self.tbl.cellWidget(i, _C.LABID)
            if w:
                w.setProperty("row", i)

    # ─── save ─────────────────────────────────────────────────────────────────

    def _sync_rows_from_table(self):
        """Read editable cells back into self._rows before saving."""
        for r, slot in enumerate(self._rows):
            # Port
            it = self.tbl.item(r, _C.PORT)
            if it:
                try:
                    slot["iport"] = int(it.text()) if it.text().strip() else None
                except ValueError:
                    slot["iport"] = None
            # Lab ID combo
            w = self.tbl.cellWidget(r, _C.LABID)
            if w:
                slot["ourlabid"] = w.currentData() or ""
                slot["sampletype"] = 4 if slot["ourlabid"] else 0
            # Checkboxes
            for col, key in ((_C.BLANK, "bisblank"),
                             (_C.REPRO, "bisreproreference"),
                             (_C.LIN,   "bislinreference")):
                it = self.tbl.item(r, col)
                if it:
                    slot[key] = it.checkState() == Qt.Checked
            # Amount
            it = self.tbl.item(r, _C.AMT)
            if it:
                try:
                    slot["freferenceamount"] = float(it.text()) if it.text().strip() else None
                except ValueError:
                    slot["freferenceamount"] = None
            # Amount uncertainty
            it = self.tbl.item(r, _C.AMT_UNC)
            if it:
                try:
                    slot["freferenceamountunc"] = float(it.text()) if it.text().strip() else None
                except ValueError:
                    slot["freferenceamountunc"] = None

    def _save(self):
        self._sync_rows_from_table()

        if self._run_only:
            # Run-only: return edited rows to caller without touching the DB
            self._dirty = False
            self.accept()
            return

        # Validate repro references before saving
        missing = self._validate_all_repro_references()
        if missing:
            reply = QMessageBox.warning(
                self, "Repro References Missing Certified Values",
                f"The following repro reference(s) lack certified values "
                f"in ReferenceControlData:\n\n"
                f"  {', '.join(missing)}\n\n"
                f"They cannot be used for calibration. Save anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        try:
            with db_manager.get_connection() as conn:
                # Replace all rows for this procedure
                conn.execute(text(
                    "DELETE FROM public.ngseqtemplate WHERE procedureid = :pid"
                ), {"pid": self._proc_id})

                # Resync the PK sequence: rows from other procedures survive the
                # DELETE and their IDs occupy low values.  If the sequence is
                # behind the table's max (common after migrations), the next
                # INSERT would try to reuse an existing ID and raise UniqueViolation.
                conn.execute(text("""
                    SELECT setval(
                        pg_get_serial_sequence('public.ngseqtemplate', 'ingseqtemplateid'),
                        COALESCE((SELECT MAX(ingseqtemplateid) FROM public.ngseqtemplate), 0)
                    )
                """))

                for slot in self._rows:
                    conn.execute(text("""
                        INSERT INTO public.ngseqtemplate
                            (procedureid, iinletid, iport, sampletype, ourlabid,
                             bisblank, bisreproreference, bislinreference,
                             freferenceamount, freferenceamountunc)
                        VALUES
                            (:pid, :pos, :port, :stype, :labid,
                             :blank, :repro, :lin, :amt, :amt_unc)
                    """), {
                        "pid":     self._proc_id,
                        "pos":     slot["iinletid"],
                        "port":    slot["iport"],
                        "stype":   slot["sampletype"],
                        "labid":   slot["ourlabid"] or None,
                        "blank":   slot["bisblank"],
                        "repro":   slot["bisreproreference"],
                        "lin":     slot["bislinreference"],
                        "amt":     slot["freferenceamount"],
                        "amt_unc": slot.get("freferenceamountunc"),
                    })

                conn.commit()

            log.info(
                f"NGSeqTemplate saved: proc={self._proc_id}, "
                f"{len(self._rows)} rows."
            )
            self._dirty = False
            self.accept()

        except Exception as e:
            log.error(f"NGSeqTemplate save failed: {e}")
            show_message(self, "Save Error", str(e), QMessageBox.Critical)

    def closeEvent(self, event):
        if self._dirty:
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                "You have unsaved changes. Close without saving?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
        event.accept()
