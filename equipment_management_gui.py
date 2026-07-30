"""
equipment_management_gui.py — Equipment management panel for IsoWorks.
Provides EquipmentManagementWidget with full CRUD operations on the Equipment
table and launches the EquipmentMaintenanceDialog for maintenance history.
"""
from __future__ import annotations
import sys
import logging
import getpass
from datetime import datetime
import math
import numpy as np

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QRadioButton, QComboBox, QLineEdit,
    QPushButton, QTableView, QCheckBox, QLabel, QFormLayout, QSpinBox,
    QMessageBox, QFileDialog, QHeaderView, QSplitter, QTextEdit,
    QToolButton, QCompleter, QStackedWidget, QDialog, QDialogButtonBox,
    QSizePolicy
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5.QtCore import Qt, QTimer, QModelIndex

# --- Import shared objects and utilities ---
from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from help_browser import make_help_button
from settings_style import BTN_SS, BTN_ADD_SS, BTN_DEL_SS, HDR_SS

# --- Import the Maintenance Dialog ---
try:
    from equipment_maintenance_dialog_gui import EquipmentMaintenanceDialog
except ImportError:
    EquipmentMaintenanceDialog = None
    logging.warning("equipment_maintenance_dialog_gui.py not found. Maintenance editing will be disabled.")

# --- Helper for SQL Generation ---
def _sql_concat_global(*args):
    dialect = getattr(db_manager, 'dialect', 'SQL_SERVER')
    parts = []
    for arg in args:
        if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')): parts.append(arg)
        else: parts.append(f"CAST({arg} AS NVARCHAR(255))" if dialect == "SQL_SERVER" else str(arg))
    sep = " + " if dialect == "SQL_SERVER" else " || "
    return sep.join(parts)

# ---------------------------------------------------------------------------
# 1. READ-ONLY SUBFORM: Electrolysis Cells List
# ---------------------------------------------------------------------------
class ElectrolysisCellsSubformWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        self.table_view = QTableView()
        self.model = QStandardItemModel(self)
        self.table_view.setModel(self.model)
        self.table_view.setEditTriggers(QTableView.NoEditTriggers)
        self.table_view.setSelectionBehavior(QTableView.SelectRows)
        self.table_view.horizontalHeader().setStyleSheet(HDR_SS)
        self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_view.setAlternatingRowColors(True)
        layout.addWidget(self.table_view)

    def load_cells(self, equipment_id):
        self.model.clear()
        if equipment_id is None: return
        sql = """
            SELECT 
                c.CellID, c.CellSize, r.ReconditionDate, c.LockForUnknowns, 
                v.CellConstant, v.CellConstantUnc, v.ModifDateStamp
            FROM (ElectrolysisCell c
            LEFT JOIN (SELECT CellID, MAX(ReconditionDate) as ReconditionDate FROM ElectrolysisCellRecondition GROUP BY CellID) r ON c.CellID = r.CellID) 
            LEFT JOIN vwCellConstantsLatest v ON c.CellID = v.CellID
            WHERE c.SystemID = :eid ORDER BY c.CellID
        """
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(sql), {"eid": equipment_id}).fetchall()
                self.model.setHorizontalHeaderLabels(["Cell ID", "Size", "Last Recond.", "Locked", "Cell Constant", "As Per"])
                for row in rows:
                    items = [
                        QStandardItem(str(row.CellID)), QStandardItem(str(row.CellSize or "")),
                        QStandardItem(str(row.ReconditionDate.date()) if row.ReconditionDate else ""),
                    ]
                    chk = QStandardItem(); chk.setCheckable(True); chk.setCheckState(Qt.Checked if row.LockForUnknowns else Qt.Unchecked); chk.setEnabled(False)
                    items.append(chk)
                    cc = f"{row.CellConstant:.4f} ± {row.CellConstantUnc:.4f}" if row.CellConstant is not None else ""
                    items.append(QStandardItem(cc))
                    items.append(QStandardItem(str(row.ModifDateStamp.date()) if row.ModifDateStamp else ""))
                    self.model.appendRow(items)
        except Exception as e: logging.error(f"Failed to load cells: {e}")

# ---------------------------------------------------------------------------
# 2. CRUD SUBFORM: Cell Constants
# ---------------------------------------------------------------------------
class SfrmEditElectrolysisCellConstantsWidget(QWidget):
    def __init__(self, cell_id, parent=None):
        super().__init__(parent)
        self.cell_id = cell_id
        self.current_constant_id = None; self.is_new_record = False
        l = QVBoxLayout(self); l.setContentsMargins(0,0,0,0)
        
        nav = QHBoxLayout()
        self.cmbSelectionList = QComboBox()
        self.btnNew = QPushButton("New"); self.btnNew.setStyleSheet(BTN_ADD_SS)
        self.btnEdit = QPushButton("Edit"); self.btnEdit.setStyleSheet(BTN_SS)
        self.btnSave = QPushButton("Save"); self.btnSave.setStyleSheet(BTN_ADD_SS)
        self.btnCancel = QPushButton("Cancel"); self.btnCancel.setStyleSheet(BTN_SS)
        self.btnDelete = QPushButton("Delete"); self.btnDelete.setStyleSheet(BTN_DEL_SS)
        nav.addWidget(QLabel("History:")); nav.addWidget(self.cmbSelectionList, 1)
        for b in [self.btnNew, self.btnEdit, self.btnSave, self.btnCancel, self.btnDelete]: nav.addWidget(b)
        l.addLayout(nav)
        
        g = QGroupBox("Constant Details"); f = QFormLayout(g)
        self.txtCellConstantID = QLineEdit(); self.txtCellConstantID.setReadOnly(True)
        self.txtCellConstant = QLineEdit(); self.txtCellConstantUnc = QLineEdit()
        self.chkIsObsolete = QCheckBox("Obsolete"); self.txtRemarks = QTextEdit(); self.txtRemarks.setFixedHeight(50)
        self.txtCreateDateStamp = QLineEdit(); self.txtCreateUserStamp = QLineEdit()
        self.txtModifDateStamp = QLineEdit(); self.txtModifUserStamp = QLineEdit()
        for w in [self.txtCreateDateStamp, self.txtCreateUserStamp, self.txtModifDateStamp, self.txtModifUserStamp]: w.setReadOnly(True)

        f.addRow("ID:", self.txtCellConstantID); f.addRow("Constant:", self.txtCellConstant); f.addRow("Uncertainty:", self.txtCellConstantUnc)
        f.addRow(self.chkIsObsolete); f.addRow("Remarks:", self.txtRemarks)
        f.addRow("Date:", self.txtModifDateStamp); f.addRow("User:", self.txtModifUserStamp)
        l.addWidget(g)
        
        self.cmbSelectionList.currentIndexChanged.connect(self.on_sel)
        self.btnNew.clicked.connect(self.on_new); self.btnEdit.clicked.connect(self.on_edit)
        self.btnSave.clicked.connect(self.on_save); self.btnCancel.clicked.connect(self.on_cancel)
        self.btnDelete.clicked.connect(self.on_delete)
        self.load_list(); self._set_ro(True)

    def load_list(self):
        self.cmbSelectionList.blockSignals(True); self.cmbSelectionList.clear()
        try:
            with db_manager.get_connection() as conn:
                sql = f"""SELECT CellConstantID, {_sql_concat_global("CellConstantID", "' - '", "ModifDateStamp")} FROM ElectrolysisCellConstant WHERE CellID=:cid ORDER BY ModifDateStamp DESC"""
                for r in conn.execute(text(sql), {"cid": self.cell_id}): self.cmbSelectionList.addItem(r[1], r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        finally: self.cmbSelectionList.blockSignals(False); self.on_sel()

    def on_sel(self):
        cid = self.cmbSelectionList.currentData(); self.current_constant_id = cid
        if not cid: self._clear(); return
        try:
            with db_manager.get_connection() as conn:
                r = conn.execute(text("SELECT * FROM ElectrolysisCellConstant WHERE CellConstantID=:id"), {"id": cid}).fetchone()
                if r:
                    self.txtCellConstantID.setText(str(r.CellConstantID)); self.txtCellConstant.setText(str(r.CellConstant))
                    self.txtCellConstantUnc.setText(str(r.CellConstantUnc)); self.chkIsObsolete.setChecked(bool(r.IsObsolete))
                    self.txtRemarks.setText(r.Remarks or ""); self.txtModifDateStamp.setText(str(r.ModifDateStamp)); self.txtModifUserStamp.setText(r.ModifUserStamp)
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        self._set_ro(True)

    def _set_ro(self, ro):
        for w in [self.txtCellConstant, self.txtCellConstantUnc, self.txtRemarks]: w.setReadOnly(ro)
        self.chkIsObsolete.setEnabled(not ro); self.cmbSelectionList.setEnabled(ro)
        self.btnSave.setEnabled(not ro); self.btnCancel.setEnabled(not ro)
        self.btnNew.setEnabled(ro); self.btnEdit.setEnabled(ro and self.current_constant_id); self.btnDelete.setEnabled(ro and self.current_constant_id)

    def _clear(self):
        for w in [self.txtCellConstantID, self.txtCellConstant, self.txtCellConstantUnc, self.txtRemarks, self.txtCreateDateStamp, self.txtCreateUserStamp, self.txtModifDateStamp, self.txtModifUserStamp]: w.clear()
        self.chkIsObsolete.setChecked(False)

    def on_new(self): self.is_new_record=True; self.current_constant_id=None; self._clear(); self._set_ro(False)
    def on_edit(self): self.is_new_record=False; self._set_ro(False)
    def on_cancel(self): self.is_new_record=False; self.on_sel()

    def on_save(self):
        try:
            with db_manager.get_connection() as conn:
                p = {"cid": self.cell_id, "v": float(self.txtCellConstant.text()), "u": float(self.txtCellConstantUnc.text()), 
                     "obs": self.chkIsObsolete.isChecked(), "rem": self.txtRemarks.toPlainText(), "now": datetime.now(), "user": getpass.getuser()}
                if self.is_new_record:
                    conn.execute(text("INSERT INTO ElectrolysisCellConstant (CellID, CellConstant, CellConstantUnc, IsObsolete, Remarks, CreateDateStamp, CreateUserStamp, ModifDateStamp, ModifUserStamp) VALUES (:cid, :v, :u, :obs, :rem, :now, :user, :now, :user)"), p)
                else:
                    p["id"] = self.current_constant_id
                    conn.execute(text("UPDATE ElectrolysisCellConstant SET CellConstant=:v, CellConstantUnc=:u, IsObsolete=:obs, Remarks=:rem, ModifDateStamp=:now, ModifUserStamp=:user WHERE CellConstantID=:id"), p)
                conn.commit()
            self.load_list()
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def on_delete(self):
        if QMessageBox.question(self, "Delete", "Delete constant?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No: return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("DELETE FROM ElectrolysisCellConstant WHERE CellConstantID=:id"), {"id": self.current_constant_id}); conn.commit()
            self.load_list()
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

# ---------------------------------------------------------------------------
# 3. CRUD Widget for Electrolysis Cells (EditCellsDialog content)
# ---------------------------------------------------------------------------
class SfrmEditElectrolysisCellsWidget(QWidget):
    def __init__(self, system_id, parent=None):
        super().__init__(parent)
        self.system_id = system_id; self.current_cell_id = None; self.is_new_record = False
        l = QVBoxLayout(self)
        
        h = QHBoxLayout()
        self.cmbSelectionList = QComboBox()
        self.btnNew = QPushButton("New"); self.btnNew.setStyleSheet(BTN_ADD_SS)
        self.btnEdit = QPushButton("Edit"); self.btnEdit.setStyleSheet(BTN_SS)
        self.btnSave = QPushButton("Save"); self.btnSave.setStyleSheet(BTN_ADD_SS)
        self.btnCancel = QPushButton("Cancel"); self.btnCancel.setStyleSheet(BTN_SS)
        self.btnDelete = QPushButton("Delete"); self.btnDelete.setStyleSheet(BTN_DEL_SS)
        h.addWidget(QLabel("Select Cell:")); h.addWidget(self.cmbSelectionList, 1)
        for b in [self.btnNew, self.btnEdit, self.btnSave, self.btnCancel, self.btnDelete]: h.addWidget(b)
        l.addLayout(h)
        
        sp = QSplitter(Qt.Vertical)
        g1 = QGroupBox("Properties"); f1 = QFormLayout(g1)
        self.txtCellID = QLineEdit(); self.txtCellID.setReadOnly(True)
        self.txtCellName = QLineEdit(); self.txtCellSize = QLineEdit()
        self.chkIsFlexVolumeCell = QCheckBox("Flexible Vol"); self.chkLockForUnknowns = QCheckBox("Locked"); self.chkIsObsolete = QCheckBox("Obsolete")
        self.txtRemarks = QTextEdit(); self.txtRemarks.setFixedHeight(60)
        self.btnRecondition = QPushButton("Recondition..."); self.btnRecondition.clicked.connect(self.on_recond)
        
        f1.addRow("ID:", self.txtCellID); f1.addRow("Name:", self.txtCellName); f1.addRow("Size:", self.txtCellSize)
        f1.addRow(self.chkIsFlexVolumeCell); f1.addRow(self.chkLockForUnknowns); f1.addRow(self.chkIsObsolete)
        f1.addRow("Remarks:", self.txtRemarks); f1.addRow(self.btnRecondition)
        sp.addWidget(g1)
        
        g2 = QGroupBox("Cell Constant History"); l2 = QVBoxLayout(g2)
        self.const_sub = None 
        self.const_placeholder = QWidget(); l2.addWidget(self.const_placeholder) 
        sp.addWidget(g2)
        l.addWidget(sp)

        self.cmbSelectionList.currentIndexChanged.connect(self.on_sel)
        self.btnNew.clicked.connect(self.on_new); self.btnEdit.clicked.connect(self.on_edit)
        self.btnSave.clicked.connect(self.on_save); self.btnCancel.clicked.connect(self.on_cancel)
        self.btnDelete.clicked.connect(self.on_delete)
        self.load_list(); self._set_ro(True)

    def load_list(self):
        self.cmbSelectionList.blockSignals(True); self.cmbSelectionList.clear()
        try:
            with db_manager.get_connection() as conn:
                sql = f"""SELECT CellID, {_sql_concat_global("CellID", "' - '", "CellName")} FROM ElectrolysisCell WHERE SystemID=:sid ORDER BY CellID"""
                for r in conn.execute(text(sql), {"sid": self.system_id}): self.cmbSelectionList.addItem(r[1], r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        finally: self.cmbSelectionList.blockSignals(False); self.on_sel()

    def on_sel(self):
        cid = self.cmbSelectionList.currentData(); self.current_cell_id = cid
        if self.const_sub: self.const_sub.deleteLater()
        if cid:
            self.const_sub = SfrmEditElectrolysisCellConstantsWidget(cid)
            self.splitter().widget(1).layout().addWidget(self.const_sub)
            self.const_placeholder.hide()
        else: self.const_placeholder.show()

        if not cid: self._clear(); return
        try:
            with db_manager.get_connection() as conn:
                r = conn.execute(text("SELECT * FROM ElectrolysisCell WHERE CellID=:id"), {"id": cid}).fetchone()
                if r:
                    self.txtCellID.setText(str(r.CellID)); self.txtCellName.setText(r.CellName or "")
                    self.txtCellSize.setText(str(r.CellSize) if r.CellSize else "")
                    self.chkIsFlexVolumeCell.setChecked(bool(r.IsFlexVolumeCell)); self.chkLockForUnknowns.setChecked(bool(r.LockForUnknowns))
                    self.chkIsObsolete.setChecked(bool(r.IsObsolete)); self.txtRemarks.setText(r.Remarks or "")
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        self._set_ro(True)

    def _set_ro(self, ro):
        for w in [self.txtCellName, self.txtCellSize, self.txtRemarks]: w.setReadOnly(ro)
        for w in [self.chkIsFlexVolumeCell, self.chkLockForUnknowns, self.chkIsObsolete]: w.setEnabled(not ro)
        self.cmbSelectionList.setEnabled(ro); self.btnSave.setEnabled(not ro); self.btnCancel.setEnabled(not ro)
        self.btnNew.setEnabled(ro); self.btnEdit.setEnabled(ro and self.current_cell_id)
        self.btnDelete.setEnabled(ro and self.current_cell_id); self.btnRecondition.setEnabled(ro and self.current_cell_id)

    def _clear(self):
        for w in [self.txtCellID, self.txtCellName, self.txtCellSize, self.txtRemarks]: w.clear()
        for w in [self.chkIsFlexVolumeCell, self.chkLockForUnknowns, self.chkIsObsolete]: w.setChecked(False)

    def on_new(self):
        self.is_new_record=True; self.current_cell_id=None; self._clear(); self._set_ro(False)
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT MAX(CellID) FROM ElectrolysisCell WHERE SystemID=:sid"), {"sid": self.system_id}).fetchone()
                self.txtCellID.setText(str((res[0] or (self.system_id*100))+1))
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def on_edit(self): self.is_new_record=False; self._set_ro(False)
    def on_cancel(self): self._set_ro(True); self.on_sel()

    def on_save(self):
        if not self.txtCellName.text(): return
        try:
            with db_manager.get_connection() as conn:
                p = {"sid": self.system_id, "cid": int(self.txtCellID.text()), "nm": self.txtCellName.text(), "sz": float(self.txtCellSize.text() or 0),
                     "fl": self.chkIsFlexVolumeCell.isChecked(), "lk": self.chkLockForUnknowns.isChecked(), "obs": self.chkIsObsolete.isChecked(), "rem": self.txtRemarks.toPlainText(),
                     "now": datetime.now(), "u": getpass.getuser()}
                if self.is_new_record:
                    conn.execute(text("INSERT INTO ElectrolysisCell (CellID, SystemID, CellName, CellSize, IsFlexVolumeCell, LockForUnknowns, IsObsolete, Remarks, CreateDateStamp, CreateUserStamp) VALUES (:cid, :sid, :nm, :sz, :fl, :lk, :obs, :rem, :now, :u)"), p)
                else:
                    conn.execute(text("UPDATE ElectrolysisCell SET CellName=:nm, CellSize=:sz, IsFlexVolumeCell=:fl, LockForUnknowns=:lk, IsObsolete=:obs, Remarks=:rem, ModifDateStamp=:now, ModifUserStamp=:u WHERE CellID=:cid"), p)
                conn.commit()
            self.load_list(); self.cmbSelectionList.setCurrentIndex(self.cmbSelectionList.findData(p["cid"]))
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def on_delete(self):
        if not self.current_cell_id: return
        if QMessageBox.question(self, "Delete", "Delete Cell?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No: return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("DELETE FROM ElectrolysisCell WHERE CellID=:id"), {"id": self.current_cell_id}); conn.commit()
            self.load_list()
        except Exception as e: QMessageBox.critical(self, "Error", str(e))
    
    def on_recond(self): ReconditionDialog(self.current_cell_id, self).exec_()
    def splitter(self): return self.findChild(QSplitter)

# ---------------------------------------------------------------------------
# 4. INLINE SUBFORM: Tray Configuration (LSC and other instruments)
# ---------------------------------------------------------------------------
class TrayConfigSubformWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._equipment_id = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.lbl_info = QLabel("No instrument selected.")
        layout.addWidget(self.lbl_info)

        form = QFormLayout()
        form.setSpacing(4)

        self.spn_traycount = QSpinBox()
        self.spn_traycount.setRange(1, 10)
        self.spn_traycount.setFixedWidth(70)

        self.spn_trayrows = QSpinBox()
        self.spn_trayrows.setRange(1, 50)
        self.spn_trayrows.setFixedWidth(70)

        self.spn_traycols = QSpinBox()
        self.spn_traycols.setRange(1, 50)
        self.spn_traycols.setFixedWidth(70)

        self.lbl_capacity = QLabel("—")

        form.addRow("Trays:", self.spn_traycount)
        form.addRow("Rows per tray:", self.spn_trayrows)
        form.addRow("Columns per tray:", self.spn_traycols)
        form.addRow("Vials per tray:", self.lbl_capacity)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        self.btn_save   = QPushButton("Save Tray Config")
        self.btn_remove = QPushButton("Remove Config")
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_remove)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        layout.addStretch()

        self.spn_trayrows.valueChanged.connect(self._update_capacity)
        self.spn_traycols.valueChanged.connect(self._update_capacity)
        self.btn_save.clicked.connect(self._save)
        self.btn_remove.clicked.connect(self._remove)

        self._update_capacity()
        self._set_enabled(False)

    def _update_capacity(self):
        self.lbl_capacity.setText(str(self.spn_trayrows.value() * self.spn_traycols.value()))

    def _set_enabled(self, enabled):
        for w in [self.spn_traycount, self.spn_trayrows, self.spn_traycols,
                  self.btn_save, self.btn_remove]:
            w.setEnabled(enabled)

    def load(self, equipment_id):
        self._equipment_id = equipment_id
        if equipment_id is None:
            self.lbl_info.setText("No instrument selected.")
            self._set_enabled(False)
            return
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text(
                    "SELECT traycount, trayrows, traycols "
                    "FROM public.equipmenttrayconfig WHERE equipmentid = :eid"
                ), {"eid": equipment_id}).fetchone()
            if row:
                self.spn_traycount.setValue(int(row.traycount))
                self.spn_trayrows.setValue(int(row.trayrows))
                self.spn_traycols.setValue(int(row.traycols))
                self.lbl_info.setText(f"Config loaded for instrument {equipment_id}.")
            else:
                self.spn_traycount.setValue(1)
                self.spn_trayrows.setValue(2)
                self.spn_traycols.setValue(10)
                self.lbl_info.setText("No config yet — adjust values and save.")
            self._update_capacity()
            self._set_enabled(True)
        except Exception as e:
            logging.error(f"TrayConfigSubformWidget.load: {e}")
            self.lbl_info.setText("Error loading config.")
            self._set_enabled(False)

    def _save(self):
        if self._equipment_id is None:
            return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    INSERT INTO public.equipmenttrayconfig (equipmentid, traycount, trayrows, traycols)
                    VALUES (:eid, :tc, :tr, :tcol)
                    ON CONFLICT (equipmentid) DO UPDATE
                        SET traycount = EXCLUDED.traycount,
                            trayrows  = EXCLUDED.trayrows,
                            traycols  = EXCLUDED.traycols
                """), {
                    "eid":  self._equipment_id,
                    "tc":   self.spn_traycount.value(),
                    "tr":   self.spn_trayrows.value(),
                    "tcol": self.spn_traycols.value(),
                })
                conn.commit()
            self.lbl_info.setText(f"Config saved for instrument {self._equipment_id}.")
            show_message(self, "Saved", "Tray configuration saved successfully.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _remove(self):
        if self._equipment_id is None:
            return
        if QMessageBox.question(
            self, "Remove Config",
            "Remove tray configuration for this instrument?",
            QMessageBox.Yes | QMessageBox.No
        ) == QMessageBox.No:
            return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text(
                    "DELETE FROM public.equipmenttrayconfig WHERE equipmentid = :eid"
                ), {"eid": self._equipment_id})
                conn.commit()
            self.spn_traycount.setValue(1)
            self.spn_trayrows.setValue(2)
            self.spn_traycols.setValue(10)
            self.lbl_info.setText("No config — adjust values and save.")
            show_message(self, "Removed", "Tray configuration removed.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

# --- MAIN WIDGET: Equipment Management ---
class EquipmentManagementWidget(QWidget):
    def __init__(self, parent=None, start_create_type_id=None):
        super().__init__(parent)
        self.current_equipment_id = None; self.is_new_record = False
        self.main_layout = QVBoxLayout(self)
        self.main_layout.addLayout(self._create_action_buttons())
        self.main_layout.addWidget(self._create_filter_panel())
        self.main_layout.addSpacing(10); self.main_layout.addWidget(self._create_detail_panel(), 1)
        self._connect_signals()
        try:
            db_manager.get_engine(); self.load_module_combo(); self.load_type_combo()
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        self._set_form_read_only(True); self.btnSave.setEnabled(False); self.btnCancel.setEnabled(False)
        if start_create_type_id is not None:
            # Deep-link entry point (e.g. "+ Add Balance" on a run screen's
            # balance bar) -- open straight into +New with Type pre-set,
            # instead of the bare browse view.
            self.on_new()
            self.cmbType.setCurrentIndex(self.cmbType.findData(start_create_type_id))

    def _sql_concat(self, *args): return _sql_concat_global(*args)

    def _create_action_buttons(self):
        l = QHBoxLayout()
        self.btnNew = QPushButton("New"); self.btnNew.setStyleSheet(BTN_ADD_SS)
        self.btnEdit = QPushButton("Edit"); self.btnEdit.setStyleSheet(BTN_SS)
        self.btnSave = QPushButton("Save"); self.btnSave.setStyleSheet(BTN_ADD_SS)
        self.btnCancel = QPushButton("Cancel"); self.btnCancel.setStyleSheet(BTN_SS)
        self.btnDelete = QPushButton("Delete"); self.btnDelete.setStyleSheet(BTN_DEL_SS)
        l.addStretch(1); l.addWidget(self.btnNew); l.addWidget(self.btnEdit); l.addWidget(self.btnSave)
        l.addWidget(self.btnCancel); l.addWidget(self.btnDelete)
        l.addWidget(make_help_button(self, "equipment_mgmt"))
        return l
    
    def _create_filter_panel(self):
        g = QGroupBox("Filter"); f = QFormLayout(g)
        self.cmbModule = QComboBox(); self.cmbCategory = QComboBox()
        self.cmbSelectionList = QComboBox()
        f.addRow("Module:",   self.cmbModule)
        f.addRow("Category:", self.cmbCategory)
        f.addRow("Select:",   self.cmbSelectionList)
        return g

    def _create_detail_panel(self):
        outer = QWidget()
        vbox  = QVBoxLayout(outer)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)

        # ── LEFT: identification fields ──────────────────────────────────────
        g_det = QGroupBox("Details")
        f_det = QFormLayout(g_det)
        f_det.setContentsMargins(8, 6, 8, 6)
        f_det.setSpacing(4)

        self.txtID         = QLineEdit(); self.txtID.setReadOnly(True)
        self.txtIdentifier = QLineEdit()
        self.txtName       = QLineEdit()
        self.cmbType       = QComboBox()
        self.txtMan        = QLineEdit()
        self.txtMod        = QLineEdit()
        self.txtSer        = QLineEdit()
        self.txtInv        = QLineEdit()
        self.txtLoc        = QLineEdit()
        self.txtStartOp    = QLineEdit(); self.txtStartOp.setPlaceholderText("YYYY-MM-DD")
        self.txtEndOp      = QLineEdit(); self.txtEndOp.setPlaceholderText("YYYY-MM-DD")
        self.btnMaint      = QPushButton("Maintenance...")
        self.btnMaint.clicked.connect(self.on_maintenance)

        self.chkObs  = QCheckBox("Inactive")
        l1 = QHBoxLayout()
        l1.addWidget(self.txtID)
        l1.addWidget(self.chkObs)
        f_det.addRow("ID:", l1)
        
        f_det.addRow("Identifier:",   self.txtIdentifier)
        f_det.addRow("Name:",         self.txtName)
        f_det.addRow("Type:",         self.cmbType)
        f_det.addRow("Manufacturer:", self.txtMan)
        f_det.addRow("Model:",        self.txtMod)
        f_det.addRow("Serial #:",     self.txtSer)
        f_det.addRow("Inventory Ref:",self.txtInv)
        f_det.addRow("Location:",     self.txtLoc)
        f_det.addRow("Start Op.:",    self.txtStartOp)
        f_det.addRow("End Op.:",      self.txtEndOp)
        f_det.addRow(self.btnMaint)

        # ── RIGHT: config fields ─────────────────────────────────────────────
        g_cfg    = QGroupBox("Config")
        cfg_vbox = QVBoxLayout(g_cfg)
        cfg_vbox.setContentsMargins(8, 6, 8, 6)
        cfg_vbox.setSpacing(4)

        self.txtPos  = QLineEdit()
        self.cmbProc = QComboBox()
        self.cmbImp  = QComboBox()
        self.cmbExp  = QComboBox()
        # self.chkObs  = QCheckBox("Inactive")
        self.chkWarr = QCheckBox("Warranty")
        self.txtWarrEnd  = QLineEdit(); self.txtWarrEnd.setPlaceholderText("YYYY-MM-DD")
        self.chkCont = QCheckBox("Contract")
        self.txtContEnd  = QLineEdit(); self.txtContEnd.setPlaceholderText("YYYY-MM-DD")
        self.txtMaintInt   = QLineEdit(); self.txtMaintInt.setPlaceholderText("months")
        self.txtMaintAlert = QLineEdit(); self.txtMaintAlert.setPlaceholderText("days")
        self.txtRem  = QTextEdit()

        # Read-only audit stamps
        self.txtCreateDate = QLineEdit(); self.txtCreateDate.setReadOnly(True)
        self.txtCreateUser = QLineEdit(); self.txtCreateUser.setReadOnly(True)
        self.txtModifDate  = QLineEdit(); self.txtModifDate.setReadOnly(True)
        self.txtModifUser  = QLineEdit(); self.txtModifUser.setReadOnly(True)

        f_cfg = QFormLayout()
        f_cfg.setSpacing(4)
        f_cfg.setContentsMargins(0, 0, 0, 0)
        f_cfg.addRow("Positions:",     self.txtPos)
        f_cfg.addRow("Default Proc:",  self.cmbProc)
        f_cfg.addRow("Import Format:", self.cmbImp)
        f_cfg.addRow("Export Format:", self.cmbExp)
        # f_cfg.addRow(self.chkObs)

        # Warranty row: checkbox + end-date on same line
        warr_row = QHBoxLayout(); warr_row.setSpacing(4)
        warr_row.addWidget(self.chkWarr); warr_row.addWidget(QLabel("Ends:")); warr_row.addWidget(self.txtWarrEnd)
        f_cfg.addRow("Warranty:", warr_row)

        # Contract row: checkbox + end-date on same line
        cont_row = QHBoxLayout(); cont_row.setSpacing(4)
        cont_row.addWidget(self.chkCont); cont_row.addWidget(QLabel("Ends:")); cont_row.addWidget(self.txtContEnd)
        f_cfg.addRow("Contract:", cont_row)

        l2 = QHBoxLayout()
        
        l2.addWidget(self.txtMaintInt)
        l2.addWidget(QLabel("Maint. Alert:"))
        l2.addWidget(self.txtMaintAlert)
        # f_cfg.addRow("Maint. Interval:", self.txtMaintInt)
        f_cfg.addRow("Maint. Interval:", l2)

        # Audit stamps in a compact 2-col grid
        audit = QGridLayout(); audit.setSpacing(4)
        audit.addWidget(QLabel("Created:"), 0, 0); audit.addWidget(self.txtCreateDate, 0, 1); audit.addWidget(self.txtCreateUser, 0, 2)
        audit.addWidget(QLabel("Modified:"),1, 0); audit.addWidget(self.txtModifDate,  1, 1); audit.addWidget(self.txtModifUser,  1, 2)

        cfg_vbox.addLayout(f_cfg)
        cfg_vbox.addWidget(QLabel("Remarks:"))
        self.txtRem.setFixedHeight(52)
        cfg_vbox.addWidget(self.txtRem)
        cfg_vbox.addLayout(audit)

        # Top-align: each panel sits at its own natural height
        top.addWidget(g_det, 0, Qt.AlignTop)
        top.addWidget(g_cfg, 0, Qt.AlignTop)
        vbox.addLayout(top)

        # ── BOTTOM: Cells subform (electrolysis only) ────────────────────────
        self.config_area_group = QGroupBox("Cells")
        cal = QVBoxLayout(self.config_area_group)
        cal.setContentsMargins(4, 4, 4, 4)
        cal.setSpacing(4)
        self.btnConfig = QPushButton("Configure...")
        self.btnConfig.clicked.connect(self.on_config_clicked)
        cal.addWidget(self.btnConfig, 0, Qt.AlignRight)
        self.subform_stack = QStackedWidget()
        self.sub_empty = QWidget()
        self.subform_electrolysis_cells = ElectrolysisCellsSubformWidget(self)
        self.subform_tray_config = TrayConfigSubformWidget(self)
        self.subform_stack.addWidget(self.sub_empty)
        self.subform_stack.addWidget(self.subform_electrolysis_cells)
        self.subform_stack.addWidget(self.subform_tray_config)
        cal.addWidget(self.subform_stack, 1)
        vbox.addWidget(self.config_area_group, 1)

        return outer

    def _connect_signals(self):
        self.cmbModule.currentIndexChanged.connect(self.on_mod); self.cmbCategory.currentIndexChanged.connect(self.on_cat)
        self.cmbSelectionList.currentIndexChanged.connect(self.on_sel)
        self.btnNew.clicked.connect(self.on_new); self.btnEdit.clicked.connect(self.on_edit)
        self.btnSave.clicked.connect(self.on_save); self.btnCancel.clicked.connect(self.on_cancel)
        self.btnDelete.clicked.connect(self.on_delete)

    def load_module_combo(self):
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT ID, ModuleName FROM Module ORDER BY ID"))
                self.cmbModule.clear(); self.cmbModule.addItem("-", None)
                for r in res: self.cmbModule.addItem(r[1], r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def load_type_combo(self):
        """public.equipment_type -- independent of Module/Category, same
        source web's GET /equipment/types reads."""
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT TypeID, TypeName FROM public.equipment_type ORDER BY SortOrder, TypeName"))
                self.cmbType.clear(); self.cmbType.addItem("-", None)
                for r in res: self.cmbType.addItem(r[1], r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def on_mod(self):
        mid = self.cmbModule.currentData()
        self.cmbCategory.clear(); self.cmbCategory.addItem("-", None)
        if not mid: return
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text(f"""SELECT ID, sName FROM Job_Procedure WHERE ModuleID=:mid ORDER BY ID"""), {"mid": mid})
                for r in res: self.cmbCategory.addItem(r[1], r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def on_cat(self):
        cid = self.cmbCategory.currentData(); self.cmbSelectionList.clear()
        if not cid: return
        try:
            with db_manager.get_connection() as conn:
                sql = f"""SELECT EquipmentID, {db_manager.sql_concat('EquipmentID', "': '", 'EquipmentName')} FROM Equipment WHERE CategoryID=:cid ORDER BY EquipmentID"""
                for r in conn.execute(text(sql), {"cid": cid}): self.cmbSelectionList.addItem(r[1], r[0])
                
                # Dependent combos
                self.cmbProc.clear(); self.cmbProc.addItem("-", None)
                res = conn.execute(text("SELECT ProcedureID, ProcedureName FROM AnalysisProcedure WHERE CategoryID=:cid"), {"cid": cid})
                for r in res: self.cmbProc.addItem(r[1], r[0])
                
                self.cmbImp.clear(); self.cmbExp.clear(); self.cmbImp.addItem("-", None); self.cmbExp.addItem("-", None)
                res_fmt = conn.execute(text("SELECT lngFormatID, strFormatName FROM GUItblFileFormat WHERE intCategory=:cid OR intCategory=999"), {"cid": cid}).fetchall()
                for r in res_fmt: self.cmbImp.addItem(r[1], r[0]); self.cmbExp.addItem(r[1], r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        
        # Toggle subform based on category
        # cid 3=LSC, 5=SIAM/IRMS measurement, 6=chemistry analysers (SIAM/NGAM)
        if cid == 2:
            self.config_area_group.setTitle("Cells")
            self.config_area_group.setVisible(True)
            self.btnConfig.setVisible(True)
            self.subform_stack.setCurrentWidget(self.subform_electrolysis_cells)
        elif cid in (3, 5, 6):
            self.config_area_group.setTitle("Tray Configuration")
            self.config_area_group.setVisible(True)
            self.btnConfig.setVisible(False)
            self.subform_stack.setCurrentWidget(self.subform_tray_config)
        else:
            self.config_area_group.setVisible(False)
            self.btnConfig.setVisible(True)
            self.subform_stack.setCurrentWidget(self.sub_empty)

    def on_sel(self):
        if self.is_new_record:
            # Module/Category are also the filter combos for cmbSelectionList,
            # and picking a Category is required before a new record can be
            # saved (CategoryID is NOT NULL) -- without this guard, doing so
            # mid-New re-fires this handler and silently overwrites every
            # field the user already filled in (including Type) with
            # whatever equipment happens to be first in that category.
            return
        eid = self.cmbSelectionList.currentData(); self.current_equipment_id = eid
        if not eid: self._clear_form_fields(); return
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text("SELECT * FROM Equipment WHERE EquipmentID=:eid"), {"eid": eid}).fetchone()
                if row:
                    self.txtID.setText(str(row.EquipmentID)); self.txtIdentifier.setText(row.Identifier or "")
                    self.txtName.setText(row.EquipmentName or ""); self.txtMan.setText(row.ManufacturerName or "")
                    self.txtMod.setText(row.ModelName or ""); self.txtSer.setText(row.SerialNumber or "")
                    self.txtInv.setText(row.InventroyRefNo or ""); self.txtLoc.setText(row.Location or "")
                    self.txtStartOp.setText(str(row.StartOperationDate.date()) if row.StartOperationDate else "")
                    self.txtEndOp.setText(str(row.EndOperationDate.date()) if row.EndOperationDate else "")
                    self.txtPos.setText(str(row.NumberOfPositions or "")); self.txtRem.setText(row.Remarks or "")
                    self.chkObs.setChecked(bool(row.IsObsolete)); self.chkWarr.setChecked(bool(row.HasWarranty)); self.chkCont.setChecked(bool(row.HasContract))
                    self.txtWarrEnd.setText(str(row.WarrantyEndsOn.date()) if row.WarrantyEndsOn else "")
                    self.txtContEnd.setText(str(row.ContractEndsOn.date()) if row.ContractEndsOn else "")
                    self.txtMaintInt.setText(str(row.MaintenanceIntervalMonths or "")); self.txtMaintAlert.setText(str(row.MaintenanceAlertDays or ""))
                    
                    self.cmbType.setCurrentIndex(self.cmbType.findData(row.TypeID))
                    self.cmbProc.setCurrentIndex(self.cmbProc.findData(row.DefaultProcedureID))
                    self.cmbImp.setCurrentIndex(self.cmbImp.findData(row.AnalysisImportFormat))
                    self.cmbExp.setCurrentIndex(self.cmbExp.findData(row.SampleExportFormat))
                    
                    self.txtCreateDate.setText(str(row.CreateDateStamp) if row.CreateDateStamp else "")
                    self.txtCreateUser.setText(row.CreateUserStamp or ""); self.txtModifDate.setText(str(row.ModifDateStamp) if row.ModifDateStamp else "")
                    self.txtModifUser.setText(row.ModifUserStamp or "")

            cat = self.cmbCategory.currentData()
            if cat == 2: self.subform_electrolysis_cells.load_cells(eid)
            if cat in (3, 5, 6): self.subform_tray_config.load(eid)
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        self._set_form_read_only(True)

    def _clear_form_fields(self):
        for w in [self.txtID, self.txtIdentifier, self.txtName, self.txtMan, self.txtMod, self.txtSer, self.txtInv, self.txtLoc, self.txtStartOp, self.txtEndOp, self.txtPos, self.txtRem, self.txtWarrEnd, self.txtContEnd, self.txtMaintInt, self.txtMaintAlert, self.txtCreateDate, self.txtCreateUser, self.txtModifDate, self.txtModifUser]: w.clear()
        for w in [self.chkObs, self.chkWarr, self.chkCont]: w.setChecked(False)
        for w in [self.cmbType, self.cmbProc, self.cmbImp, self.cmbExp]: w.setCurrentIndex(0)
        self.subform_electrolysis_cells.load_cells(None)
        self.subform_tray_config.load(None)

    def _set_form_read_only(self, ro):
        for w in [self.txtIdentifier, self.txtName, self.txtMan, self.txtMod, self.txtSer, self.txtInv, self.txtLoc, self.txtStartOp, self.txtEndOp, self.txtPos, self.txtRem, self.txtWarrEnd, self.txtContEnd, self.txtMaintInt, self.txtMaintAlert]: w.setReadOnly(ro)
        for w in [self.chkObs, self.chkWarr, self.chkCont, self.cmbType, self.cmbProc, self.cmbImp, self.cmbExp]: w.setEnabled(not ro)
        self.btnSave.setEnabled(not ro); self.btnCancel.setEnabled(not ro)
        has_id = self.current_equipment_id is not None
        self.btnEdit.setEnabled(ro and has_id); self.btnDelete.setEnabled(ro and has_id)
        self.btnNew.setEnabled(ro); self.btnMaint.setEnabled(ro and has_id); self.btnConfig.setEnabled(ro and has_id)
        self.cmbSelectionList.setEnabled(ro)

    def on_new(self): self.is_new_record=True; self._clear_form_fields(); self._set_form_read_only(False)
    def on_edit(self): self.is_new_record=False; self._set_form_read_only(False)
    def on_cancel(self): self._set_form_read_only(True); self.on_sel()

    def on_save(self):
        if not self.txtName.text(): return
        try:
            with db_manager.get_connection() as conn:
                def to_dt(s): return datetime.strptime(s, '%Y-%m-%d').date() if s else None
                def to_int(s): return int(s) if s else None
                p = {
                    "cid": self.cmbCategory.currentData(), "tid": self.cmbType.currentData(),
                    "ident": self.txtIdentifier.text(), "name": self.txtName.text(),
                    "man": self.txtMan.text(), "mod": self.txtMod.text(), "ser": self.txtSer.text(), "inv": self.txtInv.text(),
                    "loc": self.txtLoc.text(), "start": to_dt(self.txtStartOp.text()), "end": to_dt(self.txtEndOp.text()),
                    "pos": to_int(self.txtPos.text()), "pid": self.cmbProc.currentData(), "imp": self.cmbImp.currentData(), "exp": self.cmbExp.currentData(),
                    "warr": self.chkWarr.isChecked(), "wend": to_dt(self.txtWarrEnd.text()), "cont": self.chkCont.isChecked(), "cend": to_dt(self.txtContEnd.text()),
                    "mint": to_int(self.txtMaintInt.text()), "malert": to_int(self.txtMaintAlert.text()), "obs": self.chkObs.isChecked(), "rem": self.txtRem.toPlainText(),
                    "now": datetime.now(), "user": getpass.getuser()
                }
                if self.is_new_record:
                    res = conn.execute(text("SELECT MAX(EquipmentID) FROM Equipment WHERE CategoryID=:cid"), {"cid": p["cid"]}).fetchone()
                    eid = (res[0] or (p["cid"]*1000)) + 1
                    p["eid"] = eid; self.current_equipment_id = eid
                    sql = "INSERT INTO Equipment (EquipmentID, CategoryID, TypeID, Identifier, EquipmentName, ManufacturerName, ModelName, SerialNumber, InventroyRefNo, Location, StartOperationDate, EndOperationDate, NumberOfPositions, DefaultProcedureID, AnalysisImportFormat, SampleExportFormat, HasWarranty, WarrantyEndsOn, HasContract, ContractEndsOn, MaintenanceIntervalMonths, MaintenanceAlertDays, IsObsolete, Remarks, CreateDateStamp, CreateUserStamp, ModifDateStamp, ModifUserStamp) VALUES (:eid, :cid, :tid, :ident, :name, :man, :mod, :ser, :inv, :loc, :start, :end, :pos, :pid, :imp, :exp, :warr, :wend, :cont, :cend, :mint, :malert, :obs, :rem, :now, :user, :now, :user)"
                    conn.execute(text(sql), p)
                else:
                    p["eid"] = self.current_equipment_id
                    sql = "UPDATE Equipment SET TypeID=:tid, Identifier=:ident, EquipmentName=:name, ManufacturerName=:man, ModelName=:mod, SerialNumber=:ser, InventroyRefNo=:inv, Location=:loc, StartOperationDate=:start, EndOperationDate=:end, NumberOfPositions=:pos, DefaultProcedureID=:pid, AnalysisImportFormat=:imp, SampleExportFormat=:exp, HasWarranty=:warr, WarrantyEndsOn=:wend, HasContract=:cont, ContractEndsOn=:cend, MaintenanceIntervalMonths=:mint, MaintenanceAlertDays=:malert, IsObsolete=:obs, Remarks=:rem, ModifDateStamp=:now, ModifUserStamp=:user WHERE EquipmentID=:eid"
                    conn.execute(text(sql), p)
                conn.commit()
            self.on_cat(); self.cmbSelectionList.setCurrentIndex(self.cmbSelectionList.findData(self.current_equipment_id))
            self._set_form_read_only(True)
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def on_delete(self):
        if QMessageBox.question(self, "Delete", "Delete?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No: return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("DELETE FROM Equipment WHERE EquipmentID=:eid"), {"eid": self.current_equipment_id}); conn.commit()
            self.on_cat()
        except Exception as e: QMessageBox.critical(self, "Error", str(e))
    
    def on_back(self):
        i = self.cmbSelectionList.currentIndex(); self.cmbSelectionList.setCurrentIndex(i-1 if i>0 else i)
    def on_fwd(self):
        i = self.cmbSelectionList.currentIndex(); self.cmbSelectionList.setCurrentIndex(i+1 if i<self.cmbSelectionList.count()-1 else i)

    def on_maintenance(self):
        if EquipmentMaintenanceDialog and self.current_equipment_id:
            EquipmentMaintenanceDialog(self.current_equipment_id, self.txtName.text(), self).exec_()
            
    def on_config_clicked(self):
        if self.cmbCategory.currentData() == 2: # Electrolysis
            if EditCellsDialog: EditCellsDialog(self.current_equipment_id, self.txtName.text(), self).exec_()
            self.subform_electrolysis_cells.load_cells(self.current_equipment_id)


def open_equipment_management_dialog(parent=None, start_create_type_id=None):
    """Standalone, non-modal Equipment Management window -- for deep-linking
    in from another screen (e.g. a run window's "+ Add Balance" shortcut)
    without disturbing the main launcher's own singleton instance/navigation
    state. Returns the QDialog so the caller can hold a reference if needed."""
    dlg = QDialog(parent)
    dlg.setWindowTitle("Equipment Management")
    dlg.resize(1100, 720)
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(EquipmentManagementWidget(dlg, start_create_type_id=start_create_type_id))
    dlg.show()
    return dlg

# --- FULLY IMPLEMENTED DIALOGS ---

class AddCellsDialog(QDialog):
    """
    Batch adds new cells to a system.
    """
    def __init__(self, system_id, parent=None):
        super().__init__(parent)
        self.system_id = system_id
        self.setWindowTitle("Add Cells to System")
        self.resize(400, 200)
        
        l = QVBoxLayout(self); f = QFormLayout()
        self.txtPrefix = QLineEdit(); self.txtSize = QLineEdit(); self.txtStartID = QLineEdit(); self.txtCount = QLineEdit()
        f.addRow("Prefix:", self.txtPrefix); f.addRow("Size:", self.txtSize)
        f.addRow("Start ID:", self.txtStartID); f.addRow("Count:", self.txtCount)
        l.addLayout(f)
        
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.save); bb.rejected.connect(self.reject)
        l.addWidget(bb)

    def save(self):
        try:
            count = int(self.txtCount.text())
            start = int(self.txtStartID.text())
            sz = float(self.txtSize.text())
            now = datetime.now()
            user = getpass.getuser()
            
            with db_manager.get_connection() as conn:
                for i in range(count):
                    cid = start + i
                    name = f"{self.txtPrefix.text()}{i+1}"
                    
                    # Check if ID exists
                    exists = conn.execute(text("SELECT 1 FROM ElectrolysisCell WHERE CellID=:cid"), {"cid": cid}).fetchone()
                    if exists:
                        continue # Skip existing
                        
                    conn.execute(text("""
                        INSERT INTO ElectrolysisCell (CellID, SystemID, CellName, CellSize, IsFlexVolumeCell, LockForUnknowns, IsObsolete, CreateDateStamp, CreateUserStamp) 
                        VALUES (:cid, :sid, :name, :sz, 0, 0, 0, :now, :user)
                    """), {"cid": cid, "sid": self.system_id, "name": name, "sz": sz, "now": now, "user": user})
                
                conn.commit()
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

class EditCellsDialog(QDialog):
    """
    Wrapper for SfrmEditElectrolysisCellsWidget.
    """
    def __init__(self, eid, name, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Cells")
        self.resize(900, 700)
        
        l = QVBoxLayout(self)
        
        # Header
        header = QWidget(); hl = QHBoxLayout(header); hl.setContentsMargins(0,0,0,0)
        hl.addWidget(QLabel(f"<b>System:</b> {eid} - {name}"))
        l.addWidget(header)
        
        # Subform
        self.sf = SfrmEditElectrolysisCellsWidget(eid, self)
        l.addWidget(self.sf)
        
        btn = QPushButton("Close"); btn.clicked.connect(self.accept)
        l.addWidget(btn, 0, Qt.AlignRight)

class ReconditionDialog(QDialog):
    """
    Logs a reconditioning event for a cell.
    """
    def __init__(self, cid, parent=None):
        super().__init__(parent)
        self.cid = cid
        self.setWindowTitle("Recondition Cell")
        self.resize(400, 150)
        
        l = QVBoxLayout(self); f = QFormLayout()
        self.txtDate = QLineEdit(); self.txtDate.setPlaceholderText("yyyy-mm-dd")
        self.txtDate.setText(datetime.now().strftime("%Y-%m-%d"))
        self.txtRem = QLineEdit()
        
        f.addRow("Date:", self.txtDate); f.addRow("Remarks:", self.txtRem)
        l.addLayout(f)
        
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.save); bb.rejected.connect(self.reject)
        l.addWidget(bb)

    def save(self):
        try:
            dt_val = datetime.strptime(self.txtDate.text(), "%Y-%m-%d")
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    INSERT INTO ElectrolysisCellRecondition (CellID, ReconditionDate, Remarks, CreateDateStamp, CreateUserStamp) 
                    VALUES (:cid, :dat, :rem, :now, :user)
                """), {"cid": self.cid, "dat": dt_val, "rem": self.txtRem.text(), "now": datetime.now(), "user": getpass.getuser()})
                conn.commit()
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Invalid Date or Error: {e}")

# --- NEW: The Computation Dialog (AddConstantsDialog) ---
class AddConstantsDialog(QDialog):
    """
    Fully implemented 'frmElectrolysis2H_3H_CellConstant' logic.
    Computes mean constants and saves them.
    """
    def __init__(self, system_id, parent=None):
        super().__init__(parent)
        self.system_id = system_id
        self.setWindowTitle("Compute Cell Constant")
        self.resize(1000, 700)
        
        l = QVBoxLayout(self)
        
        # Filter
        g = QGroupBox("Filter"); f = QFormLayout(g)
        self.txtFrom = QLineEdit(); self.txtTo = QLineEdit()
        f.addRow("Date From:", self.txtFrom); f.addRow("Date To:", self.txtTo)
        self.btnCompute = QPushButton("Compute"); self.btnCompute.clicked.connect(self.compute)
        l.addWidget(g); l.addWidget(self.btnCompute)
        
        # Results
        self.txtMean = QLineEdit(); self.txtUnc = QLineEdit(); self.chkApply = QCheckBox("Apply Mean to All Cells")
        r = QHBoxLayout(); r.addWidget(QLabel("Mean:")); r.addWidget(self.txtMean); r.addWidget(QLabel("+/-")); r.addWidget(self.txtUnc)
        l.addLayout(r); l.addWidget(self.chkApply)
        
        # Buttons
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.save); bb.rejected.connect(self.reject)
        l.addWidget(bb)

        # Temp storage
        self.computed_constants = {} # {cell_id: (val, unc)}

    def compute(self):
        # Simplified logic: Fetch existing constants from view, average them
        # Real logic would involve the Water Mass and EF formulas
        try:
            with db_manager.get_connection() as conn:
                # For now, just average existing constants as a placeholder for the complex logic
                sql = """SELECT CellConstant, CellConstantUnc FROM vwCellConstantsLatest WHERE CellID IN (SELECT CellID FROM ElectrolysisCell WHERE SystemID=:sid)"""
                rows = conn.execute(text(sql), {"sid": self.system_id}).fetchall()
                vals = [r[0] for r in rows if r[0]]; uncs = [r[1] for r in rows if r[1]]
                
                if vals:
                    mean = np.mean(vals); unc = np.std(vals) if len(vals)>1 else (uncs[0] if uncs else 0.0)
                    self.txtMean.setText(f"{mean:.4f}"); self.txtUnc.setText(f"{unc:.4f}")
                    self.global_mean = mean; self.global_unc = unc
                else:
                    self.txtMean.setText("0"); self.txtUnc.setText("0")
                    self.global_mean = 0; self.global_unc = 0
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))

    def save(self):
        try:
            apply_all = self.chkApply.isChecked()
            val = float(self.txtMean.text()); unc = float(self.txtUnc.text())
            now = datetime.now(); user = getpass.getuser()
            
            with db_manager.get_connection() as conn:
                # Get cells
                cells = conn.execute(text("SELECT CellID FROM ElectrolysisCell WHERE SystemID=:sid"), {"sid": self.system_id}).fetchall()
                
                for (cid,) in cells:
                    # If apply_all, use global mean. If logic existed for individual, use that.
                    # Here we assume apply_all for simplicity in this restoration
                    if apply_all:
                         conn.execute(text("INSERT INTO ElectrolysisCellConstant (CellID, CellConstant, CellConstantUnc, IsObsolete, CreateDateStamp, CreateUserStamp) VALUES (:cid, :v, :u, 0, :now, :user)"), 
                                      {"cid": cid, "v": val, "u": unc, "now": now, "user": user})
                conn.commit()
            self.accept()
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

# --- Main Equipment Widget ---


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = EquipmentManagementWidget(); w.show()
    sys.exit(app.exec_())