"""
ams_wheel_template_editor_gui.py
=================================
AMS Wheel Template editor — the desktop equivalent of web's Settings >
Procedure Management > Configuration > "Edit Template..." for AMS
procedures (frontend/src/components/settings/AmsTemplateEditor.tsx).

AMS wheels are a plain 1..N ordered position list (no tray/vial grid, unlike
SI/LSC/NGAM templates) — each position just has a "type" that maps to
public.tblsampletype:

    OXI            -> 60
    OXII           -> 61
    process_blank  -> 62
    graphite_blank -> 63
    secondary_std  -> 64
    unknown        -> 0   (a plain sample slot, filled at run creation)

Stored in the same shared public.analysisprocedure_template table every
other procedure category's template uses (ordinalposition, sampletype,
templatename, portno columns only -- the tray/vial/injection columns are
irrelevant here and left at their defaults). Save is a full replace: delete
all existing rows for the procedure, then insert the current ordered list --
same semantics as web's POST /procedures/{id}/template/bulk-save.
"""
from __future__ import annotations

import logging
import getpass
from datetime import datetime
from typing import List

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QComboBox, QMessageBox,
)
from PyQt5.QtCore import Qt

from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message

log = logging.getLogger(__name__)

# (type key, display label, tblsampletype code) -- matches AmsTemplateEditor.tsx
# AMS_TYPES / TYPE_NAMEMAP / TYPE_TO_SAMPLETYPE exactly.
AMS_TYPES = [
    ("OXI", "OXI", 60),
    ("OXII", "OXII", 61),
    ("process_blank", "Process Blank", 62),
    ("graphite_blank", "Graphite Blank", 63),
    ("secondary_std", "Secondary Std", 64),
    ("unknown", "Unknown", 0),
]
_LABEL_BY_CODE = {code: label for _, label, code in AMS_TYPES}
_CODE_BY_LABEL = {label: code for _, label, code in AMS_TYPES}

_COL_POS = 0
_COL_TYPE = 1


class AmsWheelTemplateEditorDialog(QDialog):
    """Add/remove/reorder wheel positions and their sample type, then Save
    replaces the procedure's whole template in one transaction."""

    def __init__(self, procedure_id: int, procedure_name: str, parent=None):
        super().__init__(parent)
        self.procedure_id = procedure_id
        self.procedure_name = procedure_name
        self.setWindowTitle(f"AMS Wheel Template — {procedure_name}")
        self.resize(420, 560)

        self._build_ui()
        self._load()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)

        hint = QLabel(
            "Master template — saved here becomes the default wheel layout "
            "for new runs of this procedure."
        )
        hint.setStyleSheet("color:#2E7D32; font-size:11px;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Pos", "Type"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(_COL_POS, QHeaderView.Fixed)
        self.table.setColumnWidth(_COL_POS, 50)
        self.table.horizontalHeader().setSectionResizeMode(_COL_TYPE, QHeaderView.Stretch)
        root.addWidget(self.table, 1)

        add_row = QHBoxLayout()
        self.cmbAddType = QComboBox()
        for _, label, _code in AMS_TYPES:
            self.cmbAddType.addItem(label)
        add_row.addWidget(QLabel("Add:"))
        add_row.addWidget(self.cmbAddType, 1)
        self.btnAdd = QPushButton("+ Add Position")
        self.btnAdd.clicked.connect(self._add_row)
        add_row.addWidget(self.btnAdd)
        root.addLayout(add_row)

        move_row = QHBoxLayout()
        self.btnUp = QPushButton("Move Up")
        self.btnUp.clicked.connect(lambda: self._move_selected(-1))
        self.btnDown = QPushButton("Move Down")
        self.btnDown.clicked.connect(lambda: self._move_selected(1))
        self.btnRemove = QPushButton("Remove")
        self.btnRemove.setStyleSheet("color:#C62828;")
        self.btnRemove.clicked.connect(self._remove_selected)
        move_row.addWidget(self.btnUp)
        move_row.addWidget(self.btnDown)
        move_row.addStretch(1)
        move_row.addWidget(self.btnRemove)
        root.addLayout(move_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btnSave = QPushButton("Save")
        self.btnSave.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;font-weight:bold;"
            "padding:5px 18px;border-radius:4px;}"
            "QPushButton:hover{background:#219a52;}"
        )
        self.btnSave.clicked.connect(self._save)
        self.btnCancel = QPushButton("Cancel")
        self.btnCancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btnSave)
        btn_row.addWidget(self.btnCancel)
        root.addLayout(btn_row)

    # ── data ──────────────────────────────────────────────────────────────────

    def _load(self):
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT ordinalposition, sampletype
                    FROM public.analysisprocedure_template
                    WHERE procedureid = :pid
                    ORDER BY ordinalposition
                """), {"pid": self.procedure_id}).fetchall()
        except Exception as exc:
            log.error("Load AMS wheel template (procedure %s): %s", self.procedure_id, exc)
            rows = []

        self.table.setRowCount(0)
        for r in rows:
            code = r[1] if r[1] is not None else 0
            self._append_row(_LABEL_BY_CODE.get(code, "Unknown"))

    def _append_row(self, label: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        pos_item = QTableWidgetItem("")
        pos_item.setFlags(pos_item.flags() & ~Qt.ItemIsEditable)
        pos_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, _COL_POS, pos_item)

        cmb = QComboBox()
        for _, lbl, _code in AMS_TYPES:
            cmb.addItem(lbl)
        idx = cmb.findText(label)
        cmb.setCurrentIndex(idx if idx >= 0 else 0)
        self.table.setCellWidget(row, _COL_TYPE, cmb)
        self._renumber()

    def _renumber(self):
        for row in range(self.table.rowCount()):
            self.table.item(row, _COL_POS).setText(str(row + 1))

    def _add_row(self):
        self._append_row(self.cmbAddType.currentText())

    def _remove_selected(self):
        rows = sorted({idx.row() for idx in self.table.selectionModel().selectedRows()}, reverse=True)
        if not rows:
            row = self.table.currentRow()
            if row >= 0:
                rows = [row]
        for row in rows:
            self.table.removeRow(row)
        self._renumber()

    def _move_selected(self, direction: int):
        row = self.table.currentRow()
        if row < 0:
            return
        new_row = row + direction
        if new_row < 0 or new_row >= self.table.rowCount():
            return
        cmb = self.table.cellWidget(row, _COL_TYPE)
        label = cmb.currentText() if cmb else "Unknown"
        self.table.removeRow(row)
        self.table.insertRow(new_row)
        pos_item = QTableWidgetItem("")
        pos_item.setFlags(pos_item.flags() & ~Qt.ItemIsEditable)
        pos_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(new_row, _COL_POS, pos_item)
        new_cmb = QComboBox()
        for _, lbl, _code in AMS_TYPES:
            new_cmb.addItem(lbl)
        idx = new_cmb.findText(label)
        new_cmb.setCurrentIndex(idx if idx >= 0 else 0)
        self.table.setCellWidget(new_row, _COL_TYPE, new_cmb)
        self.table.setCurrentCell(new_row, _COL_TYPE)
        self._renumber()

    def _save(self):
        now = datetime.now()
        user = getpass.getuser()
        rows: List[dict] = []
        for row in range(self.table.rowCount()):
            cmb = self.table.cellWidget(row, _COL_TYPE)
            label = cmb.currentText() if cmb else "Unknown"
            code = _CODE_BY_LABEL.get(label, 0)
            pos = row + 1
            rows.append({
                "pid": self.procedure_id, "pos": pos,
                "name": self.procedure_name or "SAMPLE",
                "port": str(pos), "stype": code,
                "now": now, "user": user,
            })

        try:
            with db_manager.get_connection() as conn:
                conn.execute(text(
                    "DELETE FROM public.analysisprocedure_template WHERE procedureid = :pid"
                ), {"pid": self.procedure_id})
                for r in rows:
                    conn.execute(text("""
                        INSERT INTO public.analysisprocedure_template
                            (procedureid, ordinalposition, templatename, pageno, pageposition,
                             portno, portrefempty, sampletype, injections,
                             createdatestamp, createuserstamp, modifdatestamp, modifuserstamp)
                        VALUES
                            (:pid, :pos, :name, 0, :pos,
                             :port, :stype, :stype, 6,
                             :now, :user, :now, :user)
                    """), r)
                conn.commit()
            self.accept()
        except Exception as exc:
            log.error("Save AMS wheel template (procedure %s): %s", self.procedure_id, exc)
            show_message(self, "Error", str(exc), QMessageBox.Critical)
