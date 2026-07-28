"""
lsc_template_editor_gui.py — LSC load-list template editor dialog for IsoWorks.
Provides LscTemplateEditorDialog (QDialog) for editing the LSC position template
stored in public.analysisprocedure_template (sampletype column).
"""
import sys
import logging
import pandas as pd
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QRadioButton, QComboBox, QLineEdit,
    QPushButton, QTableView, QCheckBox, QLabel, QFormLayout,
    QMessageBox, QFileDialog, QHeaderView, QDialog, QDialogButtonBox,
    QAbstractItemView, QStyledItemDelegate, QSpinBox
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5.QtCore import Qt, QTimer

# --- Shared ---
from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message

try:
    from ui_delegates import SampleTypeDelegate
except ImportError:
    logging.warning("ui_delegates.py not found. Combobox delegate will not work.")
    SampleTypeDelegate = QStyledItemDelegate
       
class LscTemplateEditorDialog(QDialog):
    """Modal dialog to edit the LSC position template (analysisprocedure_template)."""
    def __init__(self, procedure_id, category_id, parent=None):
        super().__init__(parent)        
        self.setWindowTitle("LSC Load List Editor")
        self.setGeometry(200, 200, 600, 500)
        self.setModal(True)
        
        self.procedure_id = procedure_id
        self.category_id = category_id
        
        self.main_layout = QVBoxLayout(self)
        self.sample_type_map = self._load_sample_types()

        form_layout = QFormLayout()
        self.txtProcedureName = QLineEdit(); self.txtProcedureName.setReadOnly(True)
        self.txtNumberSamples = QLineEdit()
        self.txtCsv = QLineEdit()
        self.spnVialsPerTray = QSpinBox()
        self.spnVialsPerTray.setRange(1, 500)
        self.spnVialsPerTray.setValue(20)
        self.spnVialsPerTray.setToolTip(
            "Vials per tray (from Equipment Tray Configuration).\n"
            "Used to compute Vial# = TrayNumber-PositionInTray for each row."
        )

        form_layout.addRow("Procedure:", self.txtProcedureName)
        form_layout.addRow("Number of Positions:", self.txtNumberSamples)
        form_layout.addRow("Vials per tray:", self.spnVialsPerTray)
        form_layout.addRow("Load List String (CSV):", self.txtCsv)
        self.main_layout.addLayout(form_layout)

        table_layout = QHBoxLayout()
        self.table_view = QTableView()
        self.table_model = QStandardItemModel()
        self.table_model.setHorizontalHeaderLabels(["Position", "Vial #", "Sample Type"])
        self.table_view.setModel(self.table_model)
        self.table_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sample_type_delegate = SampleTypeDelegate(self.sample_type_map, self)
        self.table_view.setItemDelegateForColumn(2, self.sample_type_delegate)
        table_layout.addWidget(self.table_view, 3)
        
        move_button_layout = QVBoxLayout()
        self.btnMoveUp = QPushButton("Move Up"); self.btnMoveDown = QPushButton("Move Down")
        self.btnDeleteRow = QPushButton("Delete Row"); self.btnAddRow = QPushButton("Add Blank Row (0)")
        move_button_layout.addStretch()
        move_button_layout.addWidget(self.btnMoveUp); move_button_layout.addWidget(self.btnMoveDown)
        move_button_layout.addSpacing(20)
        move_button_layout.addWidget(self.btnAddRow); move_button_layout.addWidget(self.btnDeleteRow)
        move_button_layout.addStretch()
        table_layout.addLayout(move_button_layout, 1)
        self.main_layout.addLayout(table_layout, 1) 
        
        self.buttonBox = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.main_layout.addWidget(self.buttonBox)
        
        self.txtNumberSamples.editingFinished.connect(self.on_number_samples_changed)
        self.txtCsv.editingFinished.connect(self.on_csv_string_changed)
        self.spnVialsPerTray.valueChanged.connect(self._recompute_vial_labels)
        self.btnMoveUp.clicked.connect(self.on_move_up)
        self.btnMoveDown.clicked.connect(self.on_move_down)
        self.btnDeleteRow.clicked.connect(self.on_delete_row)
        self.btnAddRow.clicked.connect(self.on_add_row)
        self.buttonBox.accepted.connect(self.on_save); self.buttonBox.rejected.connect(self.reject)
        
        try:
            db_manager.get_engine()
            self.load_initial_data()
            self.update_table_from_string(self.txtCsv.text())
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    # --- SQL Helpers ---
    def _sql_concat(self, *args):
        dialect = getattr(db_manager, 'dialect', 'SQL_SERVER')
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')): parts.append(arg)
            else: parts.append(f"CAST({arg} AS NVARCHAR(255))" if dialect == "SQL_SERVER" else str(arg))
        sep = " + " if dialect == "SQL_SERVER" else " || "
        return sep.join(parts)

    def _load_sample_types(self):
        sample_map = {"0": "0: Unknown (Blank)"} 
        try:
            with db_manager.get_connection() as conn:
                # --- FIXED SYNTAX: Triple quotes ---
                sql = f"""
                    SELECT intSampleType, {db_manager.sql_concat("intSampleType", "': '", "strLongDescription")} As Description 
                    FROM tblSampleType 
                    WHERE (((intSampleType)>-1) AND ((MediaCode)=200 Or (MediaCode)=202)) OR (((MediaCode) Is Null))
                    ORDER BY intSampleType
                """
                res = conn.execute(text(sql))
                for row in res: sample_map[str(row[0])] = row[1]
            return sample_map
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return sample_map

    def load_initial_data(self):
        try:
            with db_manager.get_connection() as conn:
                proc = conn.execute(text(
                    "SELECT ProcedureName, NumberOfSamples, DefaultDeviceID FROM AnalysisProcedure WHERE ProcedureID = :pid"
                ), {"pid": self.procedure_id}).fetchone()
                if proc:
                    self.txtProcedureName.setText(proc.ProcedureName or "N/A")
                    # Auto-populate vials-per-tray from equipment tray config
                    if proc.DefaultDeviceID:
                        tc = conn.execute(text(
                            "SELECT COALESCE(trayrows,4) * COALESCE(traycols,5) AS cap "
                            "FROM public.equipmenttrayconfig WHERE equipmentid=:eid"
                        ), {"eid": proc.DefaultDeviceID}).fetchone()
                        if tc:
                            self.spnVialsPerTray.setValue(int(tc.cap))
                # Load template rows from analysisprocedure_template
                tmpl = conn.execute(text("""
                    SELECT ordinalposition, sampletype, portrefempty
                    FROM public.analysisprocedure_template
                    WHERE procedureid = :pid
                    ORDER BY ordinalposition
                """), {"pid": self.procedure_id}).fetchall()
                if tmpl:
                    csv_str = ",".join(
                        str(r.sampletype if r.sampletype is not None else (r.portrefempty or 0))
                        for r in tmpl
                    )
                    self.txtCsv.setText(csv_str)
                    self.txtNumberSamples.setText(str(len(tmpl)))
                elif proc:
                    self.txtNumberSamples.setText(str(proc.NumberOfSamples or 0))
        except Exception as e:
            logging.error(f"Failed to load initial data: {e}")

    @staticmethod
    def _vial_label(pos: int, cap: int) -> str:
        tray = (pos - 1) // cap + 1
        pit  = (pos - 1) % cap + 1
        return f"{tray}-{pit}"

    def _recompute_vial_labels(self):
        cap = self.spnVialsPerTray.value()
        for row in range(self.table_model.rowCount()):
            item = self.table_model.item(row, 1)
            if item:
                item.setText(self._vial_label(row + 1, cap))

    def update_table_from_string(self, text):
        self.table_model.clear()
        self.table_model.setHorizontalHeaderLabels(["Position", "Vial #", "Sample Type"])

        if not text:
            self.txtNumberSamples.setText("0")
            return

        text = text.replace(",", ";")
        if text.endswith(";"): text = text[:-1]
        sample_types = text.split(";")
        cap = self.spnVialsPerTray.value()

        for i, sample_type in enumerate(sample_types):
            pos  = i + 1
            pos_item  = QStandardItem(str(pos))
            pos_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            vial_item = QStandardItem(self._vial_label(pos, cap))
            vial_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            type_item = QStandardItem()
            type_item.setData(sample_type.strip(), Qt.EditRole)
            self.table_model.appendRow([pos_item, vial_item, type_item])

        self.txtNumberSamples.setText(str(len(sample_types)))
        self.table_view.resizeColumnsToContents()

    def on_csv_string_changed(self):
        self.update_table_from_string(self.txtCsv.text())
        
    def on_number_samples_changed(self):
        try: new_count = int(self.txtNumberSamples.text())
        except ValueError: new_count = 0
        current_string = self.txtCsv.text().replace(",", ";")
        if current_string.endswith(";"): current_string = current_string[:-1]
        sample_types = current_string.split(";") if current_string else []
        
        if new_count > len(sample_types): sample_types.extend(["0"] * (new_count - len(sample_types)))
        elif new_count < len(sample_types): sample_types = sample_types[:new_count]
            
        new_string = ";".join(sample_types)
        self.txtCsv.setText(new_string); self.update_table_from_string(new_string)
        
    def update_string_from_table(self):
        sample_types = []
        for row in range(self.table_model.rowCount()):
            item = self.table_model.item(row, 2)  # col 2: Sample Type
            id_str = item.data(Qt.EditRole) if item else None
            sample_types.append(str(id_str).strip() if id_str is not None else "0")
        new_string = ";".join(sample_types)
        self.txtCsv.setText(new_string)
        self.txtNumberSamples.setText(str(len(sample_types)))

    def renumber_positions(self):
        cap = self.spnVialsPerTray.value()
        for row in range(self.table_model.rowCount()):
            pos = row + 1
            self.table_model.item(row, 0).setText(str(pos))
            self.table_model.item(row, 1).setText(self._vial_label(pos, cap))

    def on_move_up(self):
        indexes = self.table_view.selectionModel().selectedRows()
        if not indexes: return
        row = indexes[0].row()
        if row > 0:
            self.table_model.insertRow(row - 1, self.table_model.takeRow(row))
            self.table_view.selectionModel().setCurrentIndex(self.table_model.index(row - 1, 0), QAbstractItemView.ClearAndSelect | QAbstractItemView.Rows)
        self.renumber_positions(); self.update_string_from_table()

    def on_move_down(self):
        indexes = self.table_view.selectionModel().selectedRows()
        if not indexes: return
        row = indexes[0].row()
        if row < self.table_model.rowCount() - 1:
            self.table_model.insertRow(row + 1, self.table_model.takeRow(row))
            self.table_view.selectionModel().setCurrentIndex(self.table_model.index(row + 1, 0), QAbstractItemView.ClearAndSelect | QAbstractItemView.Rows)
        self.renumber_positions(); self.update_string_from_table()
        
    def on_delete_row(self):
        indexes = self.table_view.selectionModel().selectedRows()
        if not indexes: return
        for index in sorted(indexes, reverse=True): self.table_model.removeRow(index.row())
        self.renumber_positions(); self.update_string_from_table()

    def on_add_row(self):
        pos  = self.table_model.rowCount() + 1
        cap  = self.spnVialsPerTray.value()
        pos_item  = QStandardItem(str(pos))
        pos_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        vial_item = QStandardItem(self._vial_label(pos, cap))
        vial_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        type_item = QStandardItem("0")
        type_item.setData("0", Qt.EditRole)
        self.table_model.appendRow([pos_item, vial_item, type_item])
        self.update_string_from_table()
        self.table_view.scrollToBottom()

    def on_save(self):
        self.update_string_from_table()
        new_string = self.txtCsv.text()
        try:
            new_count = int(self.txtNumberSamples.text())
        except ValueError:
            new_count = 0
        try:
            type_codes = [int(x.strip()) for x in new_string.replace(";", ",").split(",") if x.strip()]
        except ValueError:
            show_message(self, "Error", "Invalid type codes in load list.", QMessageBox.Warning)
            return
        cap = self.spnVialsPerTray.value()
        now = datetime.now()
        try:
            with db_manager.get_connection() as conn:
                # Get procedure name for templatename / templatemetadata
                proc = conn.execute(text(
                    "SELECT ProcedureName FROM AnalysisProcedure WHERE ProcedureID = :pid"
                ), {"pid": self.procedure_id}).fetchone()
                tmpl_name = proc.ProcedureName if proc else str(self.procedure_id)

                # Upsert templatemetadata row
                conn.execute(text("""
                    INSERT INTO public.templatemetadata (procedureid, templatename, modulecode, createdatestamp)
                    VALUES (:pid, :name, 'LSC', :now)
                    ON CONFLICT (procedureid, templatename) DO NOTHING
                """), {"pid": self.procedure_id, "name": tmpl_name, "now": now})
                tmpl_id = conn.execute(text(
                    "SELECT templateid FROM public.templatemetadata WHERE procedureid=:pid AND templatename=:name"
                ), {"pid": self.procedure_id, "name": tmpl_name}).scalar()

                # Replace all template rows atomically
                conn.execute(text(
                    "DELETE FROM public.analysisprocedure_template WHERE procedureid=:pid"
                ), {"pid": self.procedure_id})
                for idx, stype in enumerate(type_codes):
                    pos     = idx + 1
                    tray_no = idx // cap + 1
                    vial_no = idx % cap + 1
                    conn.execute(text("""
                        INSERT INTO public.analysisprocedure_template
                            (procedureid, ordinalposition, templatename, pageno, pageposition,
                             portno, trayno, vialno, portrefempty, sampletype, templateid,
                             createdatestamp, createuserstamp, modifdatestamp, modifuserstamp)
                        VALUES (:pid, :pos, :name, 0, :pos, '', :tray, :vial, :stype, :stype, :tmpl,
                                :now, 'pyqt', :now, 'pyqt')
                    """), {"pid": self.procedure_id, "pos": pos, "name": tmpl_name,
                           "tray": tray_no, "vial": vial_no, "stype": stype,
                           "tmpl": tmpl_id, "now": now})

                conn.execute(text(
                    "UPDATE AnalysisProcedure SET NumberOfSamples=:num WHERE ProcedureID=:pid"
                ), {"num": new_count, "pid": self.procedure_id})
                conn.commit()
            show_message(self, "Success", "Template saved.", QMessageBox.Information)
            self.accept()
        except Exception as e:
            show_message(self, "Database Error", f"Failed to save template:\n{e}", QMessageBox.Critical)