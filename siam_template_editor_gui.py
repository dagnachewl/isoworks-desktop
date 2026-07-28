"""
siam_template_editor_gui.py — SIAM run template editor dialog for IsoWorks.
Provides SiamTemplateEditorDialog for designing and saving SIAM load-list
templates with a visual tray widget and per-position sample-type assignment.
"""
from __future__ import annotations
import sys
import logging
import pandas as pd
from pandas import read_excel, DataFrame
import getpass
from datetime import datetime
from functools import partial

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QRadioButton, QComboBox, QLineEdit,
    QPushButton, QTableView, QCheckBox, QLabel, QFormLayout,
    QMessageBox, QFileDialog, QHeaderView, QDialog, QDialogButtonBox,
    QAbstractItemView, QSpinBox, QSplitter, QTabWidget, QToolButton,
    QMenu, QAction, QShortcut
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QFont, QBrush, QColor, QIcon, QResizeEvent, QKeySequence
from PyQt5.QtCore import Qt, QModelIndex, QItemSelection, QSize, QItemSelectionModel

# --- DB IMPORTS ---
from db_core import db_manager
from sqlalchemy import text

# --- Custom Imports ---
try:
    from ui_delegates import SampleTypeDelegate, SampleRefDelegate, SAMPLE_TYPE_COLORS
except ImportError:
    logging.error("CRITICAL: ui_delegates.py not found.")
    from PyQt5.QtWidgets import QStyledItemViewDelegate
    SampleTypeDelegate = QStyledItemViewDelegate
    SampleRefDelegate = QStyledItemViewDelegate
    SAMPLE_TYPE_COLORS = {}

try:
    from visual_tray_widgets import MultiTrayWidget, VialWidget
except ImportError:
    logging.error("CRITICAL: visual_tray_widgets.py not found.")
    MultiTrayWidget = None
    VialWidget = None

# --- Helper ---
def show_message(parent, title, text, icon=QMessageBox.Information):
    msg = QMessageBox(parent)
    msg.setWindowTitle(title)
    msg.setText(text)
    msg.setIcon(icon)
    msg.exec_()

class SiamTemplateEditorDialog(QDialog):
    
    def __init__(self, procedure_id=None, category_id=None, device_name="", parent=None, use_external_source=False):
        super().__init__(parent)
        self.setWindowTitle(f"SIAM Template Editor (Procedure: {procedure_id})")
        self.setGeometry(150, 150, 1200, 700)
        self.setModal(True)
        
        self.use_external_source = use_external_source
        self.modified_template = None
        self.procedure_id = procedure_id
        self.category_id = category_id
        self.device_name = device_name or ""
        self.db_dialect = getattr(db_manager, 'dialect', 'SQL_SERVER')
        
        self.category_prefix_map = { 3: 'T', 5: 'W', 9: 'S', 11: 'N', 14: 'N' }
        self.current_prefix = self.category_prefix_map.get(self.category_id)
        
        self.table_model = QStandardItemModel()
        self.visual_tray_widget = None
        self.vial_widgets_map = {}
        self.selected_vial = None
        self.tray_configs = [] 

        self.main_layout = QVBoxLayout(self)
        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.addWidget(self._create_template_panel_with_tools())
        self.main_splitter.addWidget(self._create_tools_panel())
        self.main_splitter.setSizes([750, 450])
        self.main_layout.addWidget(self.main_splitter, 1)
                
        self.sample_type_map = self._load_sample_types()
        self.sample_ref_map, self.sample_ref_to_type_map = self._load_sample_refs()

        self.sample_type_delegate = SampleTypeDelegate(self.sample_type_map, self)
        self.sample_ref_delegate = SampleRefDelegate(self.sample_ref_map, self)
        
        self.buttonBox = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)

        # Status bar — live row/type summary
        self._status_label = QLabel()
        self._status_label.setStyleSheet(
            "QLabel { color:#546e7a; font-size:11px; padding:2px 6px;"
            " background:#f5f5f5; border-top:1px solid #e0e0e0; }"
        )
        status_row = QHBoxLayout()
        status_row.addWidget(self._status_label, 1)
        status_row.addWidget(self.buttonBox)
        self.main_layout.addLayout(status_row)

        self._connect_signals()
        
        self.headers = ["Run Order", "Tray", "Vial", "Sample Type", "Sample ID", "Injections", "Prep", "Ignored", "Aliquot"]
        self.table_model.setHorizontalHeaderLabels(self.headers)
        self._update_col_indices()

        if not self.use_external_source:
            self.load_initial_data()
        else:
            # --- Read-Only Mode for External Source ---
            # Disable Save to prevent overwriting delicate Repeat/History logic in the main GUI
            self.buttonBox.button(QDialogButtonBox.Save).setEnabled(False)
            self.buttonBox.button(QDialogButtonBox.Cancel).setText("Close")
            self.setWindowTitle(f"SIAM Template Visualizer (Read-Only) - Procedure: {procedure_id}")
            
            # Optional: Disable editing triggers on the table to make it clear
            self.table_view.setEditTriggers(QAbstractItemView.NoEditTriggers)            
            
    def _sql_concat(self, *args):
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')): parts.append(arg)
            else: parts.append(f"CAST({arg} AS NVARCHAR(255))" if self.db_dialect == "SQL_SERVER" else str(arg))
        sep = " + " if self.db_dialect == "SQL_SERVER" else " || "
        return sep.join(parts)

    def _update_col_indices(self):
        try:
            self.sample_type_col_idx = self.headers.index("Sample Type")
            self.sample_id_col_idx = self.headers.index("Sample ID")
            self.inj_col_idx = self.headers.index("Injections")
            self.aliq_col_idx = self.headers.index("Aliquot")
            self.table_view.setItemDelegateForColumn(self.sample_type_col_idx, self.sample_type_delegate)
            self.table_view.setItemDelegateForColumn(self.sample_id_col_idx, self.sample_ref_delegate)
        except: self.sample_type_col_idx = -1
    
    def _create_config_group(self):
        group = QGroupBox("Configuration")
        layout = QFormLayout()
        self.cmbCopyFromTemplate = QComboBox(); self.btnLoadFromTemplate = QPushButton("Load")
        copy_layout = QHBoxLayout(); copy_layout.addWidget(self.cmbCopyFromTemplate, 1); copy_layout.addWidget(self.btnLoadFromTemplate)
        self.btnImportTemplate = QPushButton("Import from XLSX"); self.btnExportTemplate = QPushButton("Export to XLSX"); self.btnResetTemplate = QPushButton("Reset to Start Over")
        import_export_layout = QHBoxLayout(); import_export_layout.addStretch(); import_export_layout.addWidget(self.btnImportTemplate); import_export_layout.addWidget(self.btnExportTemplate); import_export_layout.addWidget(self.btnResetTemplate)
        layout.addRow("Copy From Existing Template:", copy_layout); layout.addRow(import_export_layout)
        group.setMaximumHeight(140); group.setLayout(layout)
        return group
        
    def _create_template_panel_with_tools(self):
        group = QGroupBox("Template (Double-click tray to add, Right-click to define)")
        main_layout = QVBoxLayout(group)

        # Procedure title sits above the table on the left
        self.lblProcedureName = QLabel(
            f"<b>Editing Procedure: {self.procedure_id}"
            f" ({self.device_name})"
            f" | Prefix: {self.current_prefix or 'N/A'}</b>"
        )
        self.lblProcedureName.setStyleSheet(
            "QLabel { color:#1565c0; font-size:12px; padding:2px 4px; }"
        )
        main_layout.addWidget(self.lblProcedureName)

        table_row = QHBoxLayout()
        self.table_view = QTableView(); self.table_view.setModel(self.table_model)
        self.table_view.setSelectionMode(QAbstractItemView.ExtendedSelection); self.table_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_view.setSortingEnabled(True); self.table_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table_row.addWidget(self.table_view, 1)
        button_layout = QVBoxLayout(); button_font = QFont(); button_font.setPointSize(14)
        self.btnMoveUp = QToolButton(); self.btnMoveUp.setText("▲"); self.btnMoveUp.setFont(button_font); self.btnMoveUp.setFixedSize(28, 28)
        self.btnMoveDown = QToolButton(); self.btnMoveDown.setText("▼"); self.btnMoveDown.setFont(button_font); self.btnMoveDown.setFixedSize(28, 28)
        self.btnDeleteRow = QToolButton(); self.btnDeleteRow.setText("X"); self.btnDeleteRow.setFont(button_font); self.btnDeleteRow.setStyleSheet("color: red;"); self.btnDeleteRow.setFixedSize(28, 28)
        button_layout.addStretch(); button_layout.addWidget(self.btnMoveUp); button_layout.addWidget(self.btnMoveDown); button_layout.addSpacing(20); button_layout.addWidget(self.btnDeleteRow); button_layout.addStretch()
        table_row.addLayout(button_layout)
        main_layout.addLayout(table_row, 1)

        # Batch Edit lives here — it acts on the table, so belongs with it
        edit_group = QGroupBox("Batch Edit Selected Rows")
        edit_row = QHBoxLayout(edit_group)
        edit_row.setContentsMargins(8, 6, 8, 6)
        self.edit_inj_spinbox = QSpinBox(); self.edit_inj_spinbox.setRange(0, 100); self.edit_inj_spinbox.setValue(6)
        self.edit_inj_spinbox.setFixedWidth(56)
        self.edit_aliq_lineedit = QLineEdit("0.0"); self.edit_aliq_lineedit.setFixedWidth(62)
        self.btnApplyToSelected = QPushButton("Apply to Selected")
        edit_row.addWidget(QLabel("Inj:")); edit_row.addWidget(self.edit_inj_spinbox)
        edit_row.addSpacing(8)
        edit_row.addWidget(QLabel("Aliquot:")); edit_row.addWidget(self.edit_aliq_lineedit)
        edit_row.addSpacing(8)
        edit_row.addWidget(self.btnApplyToSelected)
        edit_row.addStretch()
        main_layout.addWidget(edit_group)
        return group
        
    def _create_tools_panel(self):
        panel = QWidget(); layout = QVBoxLayout(panel)
        layout.addWidget(self._create_config_group())
        tray_group = QGroupBox("Visual Tray"); tray_group.setStyleSheet("QGroupBox { background-color: #F8F8F8; }")
        tray_layout = QVBoxLayout(tray_group)
        self._build_visual_trays() 
        if self.visual_tray_widget: tray_layout.addWidget(self.visual_tray_widget)
        else: tray_layout.addWidget(QLabel("Visual Tray module could not be loaded."))
        layout.addWidget(tray_group, 1)
        return panel

    def _connect_signals(self):
        self.btnLoadFromTemplate.clicked.connect(self.on_load_from_template)
        self.btnImportTemplate.clicked.connect(self.on_import_template)
        self.btnExportTemplate.clicked.connect(self.on_export_template)
        self.btnResetTemplate.clicked.connect(self.on_reset_template)
        self.btnApplyToSelected.clicked.connect(self.on_apply_to_selected)
        self.btnMoveUp.clicked.connect(self.on_move_up)
        self.btnMoveDown.clicked.connect(self.on_move_down)
        self.btnDeleteRow.clicked.connect(self.on_delete_row)
        self.buttonBox.accepted.connect(self.on_save)
        self.buttonBox.rejected.connect(self.reject)
        self.table_view.selectionModel().selectionChanged.connect(self.on_table_selection_changed)
        self.table_model.itemChanged.connect(self._on_table_item_changed)
        self.table_model.rowsInserted.connect(self._update_status)
        self.table_model.rowsRemoved.connect(self._update_status)
        self.table_model.itemChanged.connect(self._update_status)

        # Keyboard shortcuts
        QShortcut(QKeySequence(Qt.Key_Delete),       self, self.on_delete_row)
        QShortcut(QKeySequence("Ctrl+Up"),            self, self.on_move_up)
        QShortcut(QKeySequence("Ctrl+Down"),          self, self.on_move_down)

        # Right-click context menu on table
        self.table_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._on_table_context_menu)

    def _load_sample_types(self):
        sample_map = {"0": "Unknown (Blank)"} 
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("SELECT intSampleType, strLongDescription FROM tblSampleType WHERE intSampleType > -1 ORDER BY intSampleType"))
                for row in rows: sample_map[str(row[0])] = row[1]
        except Exception as e: logging.error(f"Sample type load error: {e}")
        return sample_map
            
    def _load_sample_refs(self):
        sample_ref_map = {"": ""}; sample_ref_to_type_map = {}
        try:
            with db_manager.get_connection() as conn:
                sql = f"""SELECT {db_manager.sql_concat('Prefix', "'-'", 'SampleID')} AS OurLabID, sName, SampleType FROM Sample WHERE SampleType <> 0"""
                params = {}
                if self.current_prefix: sql += " AND Prefix = :pfx"; params['pfx'] = self.current_prefix
                sql += " ORDER BY Prefix, SampleID"
                rows = conn.execute(text(sql), params)
                for row in rows:
                    our_lab_id = row[0]; display_name = row[1] or our_lab_id; sample_type_id = str(row[2])
                    sample_ref_map[our_lab_id] = display_name; sample_ref_to_type_map[our_lab_id] = sample_type_id
        except Exception as e: logging.error(f"Sample ref load error: {e}")
        return sample_ref_map, sample_ref_to_type_map
        
    def load_initial_data(self):        
        try:
            with db_manager.get_connection() as conn:
                sql = f"""SELECT ProcedureID, {db_manager.sql_concat("ProcedureID", "': '", "ProcedureName")} FROM AnalysisProcedure WHERE CategoryID = :cid AND ProcedureID <> :pid ORDER BY ProcedureID"""
                self.cmbCopyFromTemplate.clear(); self.cmbCopyFromTemplate.addItem("- Select -", None)
                rows = conn.execute(text(sql), {"cid": self.category_id, "pid": self.procedure_id})
                for row in rows: self.cmbCopyFromTemplate.addItem(row[1], row[0])
        except Exception as e: show_message(self, "Error", f"Load initial data: {e}", QMessageBox.Warning)
        self.load_template_data(self.procedure_id)
            
    def load_template_data(self, proc_id_to_load):
        self.table_model.clear(); self.table_model.setHorizontalHeaderLabels(self.headers); self._update_col_indices()
        try:
            with db_manager.get_connection() as conn:
                sql = "SELECT OrdinalPosition, TrayNo, VialNo, PortRefEmpty, RefOurLabID, Injections, PrepInjections, IgnoredInjections, AliquotSize FROM AnalysisProcedure_Template WHERE ProcedureID = :pid ORDER BY OrdinalPosition"
                rows = conn.execute(text(sql), {"pid": proc_id_to_load}).fetchall()
                for row in rows: self.table_model.appendRow(self._create_table_row(row))
            self._finalize_loading()
        except Exception as e: show_message(self, "Error", f"Load template: {e}", QMessageBox.Warning)

    def load_from_model(self, source_model):
        self.table_model.clear(); self.table_model.setHorizontalHeaderLabels(self.headers); self._update_col_indices()
        self.table_model.blockSignals(True)
        
        for row in range(source_model.rowCount()):
            pos = source_model.item(row, 0).data(Qt.DisplayRole)
            tray = source_model.item(row, 1).data(Qt.DisplayRole)
            vial = source_model.item(row, 2).data(Qt.DisplayRole)
            prefix = source_model.item(row, 3).text()
            sample_id = source_model.item(row, 4).text()
            
            sample_type_raw = source_model.item(row, 7).data(Qt.DisplayRole)
            sample_type = int(sample_type_raw) if sample_type_raw is not None else 0
            
            full_id = f"{prefix}-{sample_id}" if (sample_id and prefix) else sample_id
            if full_id: self.sample_ref_to_type_mapfull_id = str(sample_type)

            pos_item = QStandardItem(); pos_item.setData(pos, Qt.DisplayRole)
            tray_item = QStandardItem(); tray_item.setData(tray, Qt.DisplayRole)
            vial_item = QStandardItem(); vial_item.setData(vial, Qt.DisplayRole)

            type_item = QStandardItem()
            type_item.setData(int(sample_type), Qt.EditRole) 
            type_item.setText(self.sample_type_map.get(str(sample_type), str(sample_type)))

            id_item = QStandardItem(); id_item.setData(full_id, Qt.EditRole); id_item.setText(full_id)
            inj_item = QStandardItem(); inj_item.setData(6, Qt.DisplayRole)
            prep_item = QStandardItem(); prep_item.setData(0, Qt.DisplayRole)
            ignored_item = QStandardItem(); ignored_item.setData(0, Qt.DisplayRole)
            aliq_item = QStandardItem(); aliq_item.setData(0.0, Qt.DisplayRole)
            
            # Update: pass full_id to styling
            self._apply_row_styling(type_item, id_item, str(sample_type), full_id)
            self.table_model.appendRow([pos_item, tray_item, vial_item, type_item, id_item, inj_item, prep_item, ignored_item, aliq_item])
            
        self.table_model.blockSignals(False)
        self._finalize_loading()

    def _finalize_loading(self):
        self._resize_columns()
        self.sync_tray_from_template()
        self._update_status()

    def _resize_columns(self):
        header = self.table_view.horizontalHeader()
        for i in range(self.table_model.columnCount()):
            if i == self.sample_type_col_idx: header.setSectionResizeMode(i, QHeaderView.Interactive)
            else: header.setSectionResizeMode(i, QHeaderView.Stretch)
        self.table_view.resizeColumnToContents(self.sample_type_col_idx)

    def _create_table_row(self, db_row=None):
        if db_row:
            pos = int(db_row.OrdinalPosition); tray = int(db_row.TrayNo or 1); vial = int(db_row.VialNo or 0)
            sample_id = db_row.RefOurLabID or ""
            sample_type = self.sample_ref_to_type_map.get(sample_id, "0")
            inj = int(db_row.Injections or 6); prep = int(db_row.PrepInjections or 0)
            ignored = int(db_row.IgnoredInjections or 0); aliquot = float(db_row.AliquotSize or 0.0)
        else:
            pos = self.table_model.rowCount() + 1; tray = 1; vial = pos
            sample_type = "0"; sample_id = ""; inj = 6; prep = 0; ignored = 0; aliquot = 0.0
            
        pos_item = QStandardItem(); pos_item.setData(pos, Qt.DisplayRole)
        tray_item = QStandardItem(); tray_item.setData(tray, Qt.DisplayRole)
        vial_item = QStandardItem(); vial_item.setData(vial, Qt.DisplayRole)
        
        type_item = QStandardItem(); type_item.setData(int(sample_type), Qt.EditRole)
        type_item.setText(self.sample_type_map.get(sample_type, sample_type))
        id_item = QStandardItem(); id_item.setData(sample_id, Qt.EditRole); id_item.setText(sample_id)
        inj_item = QStandardItem(); inj_item.setData(inj, Qt.DisplayRole)
        prep_item = QStandardItem(); prep_item.setData(prep, Qt.DisplayRole)
        ignored_item = QStandardItem(); ignored_item.setData(ignored, Qt.DisplayRole)
        aliq_item = QStandardItem(); aliq_item.setData(aliquot, Qt.DisplayRole)
        
        # Update: pass sample_id to styling
        self._apply_row_styling(type_item, id_item, sample_type, sample_id)
        return [pos_item, tray_item, vial_item, type_item, id_item, inj_item, prep_item, ignored_item, aliq_item]
    
    # --- ADDED: single colour-of-truth used by both table and tray --------
    def _get_type_color(self, sample_type_id, sample_id_text=""):
        """Return (bg_hex, is_bold) matching _apply_row_styling logic."""
        sid = str(sample_type_id) if sample_type_id is not None else "0"
        if sid == "0" and sample_id_text:
            return "#90CAF9", True
        if sid == "0":
            return SAMPLE_TYPE_COLORS.get("0", None), False
        return SAMPLE_TYPE_COLORS.get(sid, None), True

    # --- ADDED: live status bar -------------------------------------------
    def _update_status(self, *_):
        if not hasattr(self, "_status_label"):
            return
        n = self.table_model.rowCount()
        if n == 0 or self.sample_type_col_idx < 0:
            self._status_label.setText("No rows in template.")
            return

        # A vial may appear more than once (multiple injections).
        # Count unique (Tray, Vial) positions per sample type so the summary
        # reflects physical vial slots, not injection repetitions.
        try:
            tray_col = self.headers.index("Tray")
            vial_col = self.headers.index("Vial")
        except ValueError:
            self._status_label.setText(f"Rows: {n}")
            return

        seen: dict = {}   # (tray, vial) -> type_label  — last write wins
        for r in range(n):
            tray_item = self.table_model.item(r, tray_col)
            vial_item = self.table_model.item(r, vial_col)
            type_item = self.table_model.item(r, self.sample_type_col_idx)
            if not (tray_item and vial_item and type_item):
                continue
            key   = (tray_item.data(Qt.DisplayRole), vial_item.data(Qt.DisplayRole))
            label = type_item.text() or "Unknown"
            seen[key] = label

        counts: dict = {}
        for label in seen.values():
            counts[label] = counts.get(label, 0) + 1

        unique_vials = len(seen)
        summary = "  |  ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        self._status_label.setText(
            f"Rows: {n}  ·  Unique vials: {unique_vials}   —   {summary}"
        )

    # --- ADDED: table right-click context menu ----------------------------
    def _on_table_context_menu(self, pos):
        rows = self.table_view.selectionModel().selectedRows()
        if not rows:
            return
        menu = QMenu(self)
        menu.addAction("Move Up   (Ctrl+↑)",  self.on_move_up)
        menu.addAction("Move Down (Ctrl+↓)",  self.on_move_down)
        menu.addSeparator()
        type_menu = menu.addMenu("Set Sample Type")
        for type_id, label in sorted(self.sample_type_map.items(), key=lambda x: int(x[0])):
            color, bold = self._get_type_color(type_id)
            action = QAction(label, self)
            if color:
                action.setIcon(self._color_icon(color))
            action.triggered.connect(partial(self._set_type_on_selected, type_id))
            type_menu.addAction(action)
        menu.addSeparator()
        menu.addAction("Delete Row(s)  (Del)", self.on_delete_row)
        menu.exec_(self.table_view.viewport().mapToGlobal(pos))

    def _color_icon(self, hex_color: str):
        """Tiny coloured square icon for context menu entries."""
        from PyQt5.QtGui import QPixmap, QPainter
        pix = QPixmap(14, 14)
        pix.fill(QColor(hex_color))
        painter = QPainter(pix)
        painter.setPen(QColor("#999"))
        painter.drawRect(0, 0, 13, 13)
        painter.end()
        return QIcon(pix)

    def _set_type_on_selected(self, type_id: str):
        rows = self.table_view.selectionModel().selectedRows()
        if not rows:
            return
        self.table_model.blockSignals(True)
        for idx in rows:
            r = idx.row()
            type_item = self.table_model.item(r, self.sample_type_col_idx)
            id_item   = self.table_model.item(r, self.sample_id_col_idx)
            if type_item and id_item:
                type_item.setData(int(type_id), Qt.EditRole)
                type_item.setText(self.sample_type_map.get(type_id, type_id))
                self._apply_row_styling(type_item, id_item, type_id, id_item.text())
        self.table_model.blockSignals(False)
        self.sync_tray_from_template()

    # --- UPDATED: Consistent Color Coding & Darker Blue for Assigned Unknowns ---
    def _apply_row_styling(self, type_item, id_item, sample_type, sample_id_text):
            font = QFont()
            transparent = QBrush(QColor("transparent"))

            # Reset to clean state
            type_item.setBackground(transparent); id_item.setBackground(transparent)
            type_item.setFont(font); id_item.setFont(font)

            color, bold = self._get_type_color(sample_type, sample_id_text)
            if bold:
                font.setBold(True)
            brush = QBrush(QColor(color)) if color else None

            if brush:
                type_item.setBackground(brush)
                id_item.setBackground(brush)

            type_item.setFont(font)
            id_item.setFont(font)

    def _on_table_item_changed(self, item):
        if self.table_model.signalsBlocked(): return
        row = item.row()
        if item.column() == self.sample_id_col_idx:
            new_id = item.text(); new_type = self.sample_ref_to_type_map.get(new_id, "0")
            type_item = self.table_model.item(row, self.sample_type_col_idx)
            self.table_model.blockSignals(True)
            type_item.setData(int(new_type), Qt.EditRole); type_item.setText(self.sample_type_map.get(new_type, new_type))
            
            # Update styling with new ID
            self._apply_row_styling(type_item, item, new_type, new_id)
            self.table_model.blockSignals(False)
            self.sync_tray_from_template()
        elif item.column() == self.sample_type_col_idx:
            new_type = str(item.data(Qt.EditRole)); id_item = self.table_model.item(row, self.sample_id_col_idx)
            self.table_model.blockSignals(True)
            
            # Update styling with current ID
            self._apply_row_styling(item, id_item, new_type, id_item.text())
            self.table_model.blockSignals(False)
            self.sync_tray_from_template()
        elif item.column() in [1, 2]: self.sync_tray_from_template()

    def _build_visual_trays(self):
        if MultiTrayWidget is None: return
        dev_name = self.device_name.lower()
        if "picarro 2024" in dev_name or "a0340" in dev_name: self.tray_configs = [(13, 10, "Numeric")]
        elif "picarro" in dev_name: self.tray_configs = [(15, 7, "Numeric")]
        elif "lgr" in dev_name or "los gatos" in dev_name: self.tray_configs = [(6, 9, "Numeric"), (6, 9, "Numeric"), (6, 9, "Numeric")]
        else: self.tray_configs = [(8, 12, "AlphaNumeric")]
        self.visual_tray_widget = MultiTrayWidget(self.tray_configs)
        self.vial_widgets_map = self.visual_tray_widget.get_all_vials()
        for (tray_id, pos), vial in self.vial_widgets_map.items():
            vial.vialLeftClicked.connect(self.on_vial_left_clicked)
            vial.vialRightClicked.connect(self.on_vial_right_clicked)
            vial.vialDoubleClicked.connect(self.on_vial_double_clicked)

    def sync_tray_from_template(self):
        if not self.vial_widgets_map: return
        for vial in self.vial_widgets_map.values(): vial.setData(None, None, str(vial.position))
        if self.sample_type_col_idx == -1: return
        
        tray_col = self.headers.index("Tray"); vial_col = self.headers.index("Vial"); id_col = self.headers.index("Sample ID")
        type_col = self.headers.index("Sample Type")

        for row in range(self.table_model.rowCount()):
            try:
                tray_id = self.table_model.item(row, tray_col).data(Qt.DisplayRole)
                vial_pos = self.table_model.item(row, vial_col).data(Qt.DisplayRole)
                vial_widget = self.visual_tray_widget.get_vial(int(tray_id), str(vial_pos))
                if vial_widget:
                    sample_id = self.table_model.item(row, id_col).data(Qt.EditRole)
                    sample_type_id = self.sample_ref_to_type_map.get(sample_id)
                    if sample_type_id is None:
                        sample_type_id = str(self.table_model.item(row, type_col).data(Qt.EditRole) or "0")
                    display_text = sample_id or str(vial_pos)
                    if display_text and "-" in display_text:
                        parts = display_text.split("-", 1)
                        if len(parts) == 2: display_text = f"{parts[0]}\n{parts[1]}"
                    vial_widget.setData(sample_type_id, sample_id, display_text)
                    # Push matching colour so tray mirrors the table
                    color, _ = self._get_type_color(sample_type_id, sample_id or "")
                    if color:
                        try:
                            vial_widget.set_color(color)
                        except AttributeError:
                            vial_widget.setStyleSheet(
                                f"background-color:{color}; border-radius:3px;"
                            )
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        self._update_status()

    def on_vial_left_clicked(self, tray_id, vial_pos):
        vial_widget = self.visual_tray_widget.get_vial(tray_id, str(vial_pos))
        if not vial_widget or vial_widget.sample_type_id is None:
            return  # empty vial — do nothing

        tray_col = self.headers.index("Tray")
        vial_col = self.headers.index("Vial")

        sel_model = self.table_view.selectionModel()
        sel_model.clearSelection()
        first_found = True

        for row in range(self.table_model.rowCount()):
            r_tray = self.table_model.item(row, tray_col).data(Qt.DisplayRole)
            r_vial = self.table_model.item(row, vial_col).data(Qt.DisplayRole)
            if int(r_tray) == int(tray_id) and str(r_vial) == str(vial_pos):
                # Additive select: preserves previously highlighted rows
                idx = self.table_model.index(row, 0)
                sel_model.select(
                    idx,
                    QItemSelectionModel.Select | QItemSelectionModel.Rows
                )
                if first_found:
                    self.table_view.scrollTo(idx)
                    first_found = False

    def on_vial_double_clicked(self, tray_id, vial_pos):
        vial_widget = self.visual_tray_widget.get_vial(tray_id, str(vial_pos))
        if not vial_widget or vial_widget.sample_type_id is None: return
        new_row = self._create_table_row(None)
        new_row[0].setData(self.table_model.rowCount() + 1, Qt.DisplayRole)
        new_row[1].setData(tray_id, Qt.DisplayRole)
        new_row[2].setData(vial_pos, Qt.DisplayRole)
        stype = vial_widget.sample_type_id
        new_row[3].setData(stype, Qt.EditRole); new_row[3].setText(self.sample_type_map.get(stype, stype))
        new_row[4].setData(vial_widget.sample_id, Qt.EditRole); new_row[4].setText(vial_widget.sample_id or "")
        self._apply_row_styling(new_row[3], new_row[4], stype, vial_widget.sample_id)
        self.table_model.appendRow(new_row); self.table_view.scrollToBottom(); self.table_view.selectRow(self.table_model.rowCount() - 1)
        self.table_model.blockSignals(False)
        self.sync_tray_from_template()

    def on_vial_right_clicked(self, tray_id, vial_pos, global_pos):
        vial_widget = self.visual_tray_widget.get_vial(tray_id, str(vial_pos))
        if not vial_widget: return
        menu = QMenu(self)
        menu.addAction("Set as Unknown (0)", partial(self.set_vial_and_update_template, vial_widget, "0", None, str(vial_pos)))
        menu.addSeparator()
        sub = menu.addMenu("Assign Sample")
        for sid, name in sorted(self.sample_ref_map.items()):
            if not sid: continue
            stype = self.sample_ref_to_type_map.get(sid, "0")
            sub.addAction(f"{sid} ({name})", partial(self.set_vial_and_update_template, vial_widget, stype, sid, sid))
        menu.exec_(global_pos)
        
    def set_vial_and_update_template(self, vial_widget, sample_type_id, sample_id, display_text):
        if display_text and "-" in display_text:
             parts = display_text.split("-", 1)
             if len(parts) == 2: display_text = f"{parts[0]}\n{parts[1]}"
        vial_widget.setData(sample_type_id, sample_id, display_text)
        self.sync_template_from_vial(vial_widget)
        
    def sync_template_from_vial(self, vial_widget):    
        if not self.headers: return
        tray_col, vial_col = self.headers.index("Tray"), self.headers.index("Vial")
        self.table_model.blockSignals(True)
        for row in range(self.table_model.rowCount()):
            r_tray = self.table_model.item(row, tray_col).data(Qt.DisplayRole)
            r_vial = self.table_model.item(row, vial_col).data(Qt.DisplayRole)
            if int(r_tray) == int(vial_widget.tray_id) and str(r_vial) == str(vial_widget.position):
                type_item = self.table_model.item(row, self.sample_type_col_idx)
                id_item = self.table_model.item(row, self.sample_id_col_idx)
                type_item.setData(vial_widget.sample_type_id, Qt.EditRole)
                type_item.setText(self.sample_type_map.get(vial_widget.sample_type_id, vial_widget.sample_type_id))
                id_item.setData(vial_widget.sample_id, Qt.EditRole); id_item.setText(vial_widget.sample_id)
                
                # Pass ID to styling
                self._apply_row_styling(type_item, id_item, vial_widget.sample_type_id, vial_widget.sample_id)
        self.table_model.blockSignals(False)
        
    def on_load_from_template(self):
        proc_id = self.cmbCopyFromTemplate.currentData()
        if proc_id is None: show_message(self, "Info", "Select a template.", QMessageBox.Information); return
        if QMessageBox.question(self, "Confirm", "Replace current template?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.Yes:
            self.load_template_data(proc_id); self.sync_tray_from_template()

    def on_import_template(self):
        fpath, _ = QFileDialog.getOpenFileName(self, "Import", "", "Excel (*.xlsx)")
        if not fpath: return
        try:
            df = pd.read_excel(fpath, dtype={'Sample ID': str})
            if list(df.columns) != self.headers: show_message(self, "Error", f"Header mismatch.\nExpected: {self.headers}", QMessageBox.Critical); return
            if QMessageBox.question(self, "Confirm", f"Replace with {len(df)} rows?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No: return
            self.table_model.clear(); self.table_model.setHorizontalHeaderLabels(self.headers); self._update_col_indices()
            self.table_model.blockSignals(True)
            from collections import namedtuple
            DBRow = namedtuple("DBRow", ["OrdinalPosition", "TrayNo", "VialNo", "PortRefEmpty", "RefOurLabID", "Injections", "PrepInjections", "IgnoredInjections", "AliquotSize"])
            for row in df.itertuples():
                drow = DBRow(
                    OrdinalPosition=getattr(row, "Run Order"), TrayNo=getattr(row, "Tray"), VialNo=getattr(row, "Vial"),
                    PortRefEmpty=None, RefOurLabID=str(getattr(row, "Sample ID") or ""),
                    Injections=getattr(row, "Injections"), PrepInjections=getattr(row, "Prep"),
                    IgnoredInjections=getattr(row, "Ignored"), AliquotSize=getattr(row, "Aliquot")
                )
                self.table_model.appendRow(self._create_table_row(drow))
            self.table_model.blockSignals(False); self.renumber_positions(); self.sync_tray_from_template()
            show_message(self, "Success", f"Imported {len(df)} rows.")
        except Exception as e: show_message(self, "Error", str(e), QMessageBox.Critical)

    def on_export_template(self):
        fpath, _ = QFileDialog.getSaveFileName(self, "Export", "template.xlsx", "Excel (*.xlsx)")
        if not fpath: return
        try:
            data = []
            for r in range(self.table_model.rowCount()):
                row_data = {}
                for c, h in enumerate(self.headers):
                    item = self.table_model.item(r, c)
                    if c == self.sample_id_col_idx: row_datah = item.data(Qt.EditRole)
                    elif c == self.sample_type_col_idx: row_datah = item.text()
                    else: row_datah = item.data(Qt.DisplayRole)
                data.append(row_data)
            pd.DataFrame(data, columns=self.headers).to_excel(fpath, index=False)
            show_message(self, "Success", f"Exported to {fpath}")
        except Exception as e: show_message(self, "Error", str(e), QMessageBox.Critical)

    def on_reset_template(self):
        if QMessageBox.question(self, "Reset", "Clear and reset template?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.Yes:
            self.table_model.clear(); self.table_model.setHorizontalHeaderLabels(self.headers); self._update_col_indices()
            for i in range(10): r = self._create_table_row(None); r[0].setData(i+1, Qt.DisplayRole); self.table_model.appendRow(r)
            self.sync_tray_from_template()
            
    def on_apply_to_selected(self):       
        rows = self.table_view.selectionModel().selectedRows()
        if not rows: return
        try: 
            aliq = float(self.edit_aliq_lineedit.text()); inj = self.edit_inj_spinbox.value()
            self.table_model.blockSignals(True)
            for idx in rows:
                self.table_model.item(idx.row(), self.inj_col_idx).setData(inj, Qt.DisplayRole)
                self.table_model.item(idx.row(), self.aliq_col_idx).setData(aliq, Qt.DisplayRole)
            self.table_model.blockSignals(False)
        except: show_message(self, "Error", "Invalid Aliquot", QMessageBox.Warning)

    def on_table_selection_changed(self, selected, deselected):        
        if self.selected_vial: self.selected_vial.set_selected(False); self.selected_vial = None
        rows = self.table_view.selectionModel().selectedRows()
        if rows:
            r = rows[0].row()
            try:
                tray = int(self.table_model.item(r, self.headers.index("Tray")).data(Qt.DisplayRole))
                vial = self.table_model.item(r, self.headers.index("Vial")).data(Qt.DisplayRole)
                vw = self.visual_tray_widget.get_vial(tray, str(vial))
                if vw: vw.set_selected(True); self.selected_vial = vw
            except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def renumber_positions(self):       
        for r in range(self.table_model.rowCount()): self.table_model.item(r, 0).setData(r + 1, Qt.DisplayRole)
    def on_move_up(self): self._move_rows(-1)
    def on_move_down(self): self._move_rows(1)
    def _move_rows(self, direction):
        rows = sorted(list(set(i.row() for i in self.table_view.selectionModel().selectedRows())), reverse=(direction>0))
        if not rows: return
        if direction < 0 and rows[-1] == 0: return
        if direction > 0 and rows[0] == self.table_model.rowCount() - 1: return
        selection_ranges = []
        for r in rows:
            items = self.table_model.takeRow(r); new_idx = r + direction
            self.table_model.insertRow(new_idx, items); selection_ranges.append(new_idx)
        self.table_view.selectionModel().clear()
        for r in selection_ranges: self.table_view.selectRow(r)
        self.renumber_positions()
    def on_delete_row(self):
        for r in sorted(list(set(i.row() for i in self.table_view.selectionModel().selectedRows())), reverse=True): self.table_model.removeRow(r)            
        self.renumber_positions(); self.sync_tray_from_template()

    def _resolve_type_id(self, raw_type) -> int:
        """Always return an integer type ID regardless of whether raw_type is
        already an int/digit-string or a description string like 'DI Wash Sample'."""
        if raw_type is None:
            return 0
        s = str(raw_type).strip()
        if s.lstrip("-").isdigit():
            return int(s)
        # Description string → reverse-lookup in sample_type_map
        desc_to_id = {v: k for k, v in self.sample_type_map.items()}
        return int(desc_to_id.get(s, 0))

    def on_save(self):
        if self.table_model.rowCount() == 0:
            show_message(self, "Empty Template",
                         "The template has no rows. Add at least one position before saving.",
                         QMessageBox.Warning)
            return
        if self.use_external_source:
            self.modified_template = []
            for r in range(self.table_model.rowCount()):
                itm = lambda c: self.table_model.item(r, self.headers.index(c))
                sample_id  = itm("Sample ID").data(Qt.EditRole) or ""
                raw_type   = itm("Sample Type").data(Qt.EditRole)
                final_type = self._resolve_type_id(
                    self.sample_ref_to_type_map.get(sample_id) or raw_type
                )
                self.modified_template.append({
                    "Run Order": itm("Run Order").data(Qt.DisplayRole), "Tray": itm("Tray").data(Qt.DisplayRole),
                    "Vial": itm("Vial").data(Qt.DisplayRole), "Sample Type": final_type,
                    "Sample ID": sample_id, "Injections": itm("Injections").data(Qt.DisplayRole),
                    "Prep": itm("Prep").data(Qt.DisplayRole), "Ignored": itm("Ignored").data(Qt.DisplayRole),
                    "Aliquot": itm("Aliquot").data(Qt.DisplayRole)
                })
            self.accept()
        else:
            try:
                with db_manager.get_connection() as conn:
                    conn.execute(text("DELETE FROM AnalysisProcedure_Template WHERE ProcedureID = :pid"), {"pid": self.procedure_id})
                    rows_to_insert = []; now_val = datetime.now(); user_val = getpass.getuser()
                    for r in range(self.table_model.rowCount()):
                        itm = lambda c: self.table_model.item(r, self.headers.index(c))
                        def get_val(col, role=Qt.DisplayRole, default=None):
                            item = itm(col); val = item.data(role)
                            return val if val is not None else default
                        rows_to_insert.append({
                            "pid": self.procedure_id, "pos": get_val("Run Order", default=r+1), "tname": f"Template {self.procedure_id}",
                            "page": 1, "ppos": get_val("Run Order"), "port": f"{get_val('Tray')}-{get_val('Vial')}",
                            "tray": int(get_val("Tray") or 1), "vial": int(get_val("Vial") or 0),
                            "type": self._resolve_type_id(get_val("Sample Type", Qt.EditRole)),
                            "inj": int(get_val("Injections") or 6), "prep": int(get_val("Prep") or 0), "ign": int(get_val("Ignored") or 0),
                            "aliq": float(get_val("Aliquot") or 0.0), "ref": get_val("Sample ID", Qt.EditRole) or "", "now": now_val, "user": user_val
                        })
                    sql = text("""INSERT INTO AnalysisProcedure_Template (ProcedureID, OrdinalPosition, TemplateName, PageNo, PagePosition, PortNo, TrayNo, VialNo, PortRefEmpty, Injections, PrepInjections, IgnoredInjections, AliquotSize, RefOurLabID, CreateDateStamp, CreateUserStamp) VALUES (:pid, :pos, :tname, :page, :ppos, :port, :tray, :vial, :type, :inj, :prep, :ign, :aliq, :ref, :now, :user)""")
                    conn.execute(sql, rows_to_insert); conn.commit()
                show_message(self, "Success", "Template saved."); self.accept()
            except Exception as e: show_message(self, "Error", f"Failed to save: {e}", QMessageBox.Critical)