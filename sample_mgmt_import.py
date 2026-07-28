# -*- coding: utf-8 -*-
"""
Sample Management – Import Samples (CSV/XLSX) → map → preview → validate → commit to LIMS.
Refactored to use db_core (SQLAlchemy).
"""

from __future__ import annotations
import os, sys
import logging
import datetime as dt
from typing import Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import text

from PyQt5.QtCore import Qt, QSettings
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QGroupBox, QFormLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog, QComboBox, QTableWidget, QDialog,
    QTableWidgetItem, QMessageBox, QCheckBox, QSpinBox, QApplication, QDialogButtonBox
)

# --- Import shared DB manager ---
from db_core import db_manager

TARGET_FIELDS = [
    "Prefix", "SampleID", "SampleName", "Matrix",
    "Project", "Owner", "ReceivedDate", "Remarks",
    "ContainerType", "SampleVolume",
]

HEADER_ALIASES = {
    "Prefix":        ["prefix", "prfx"],
    "SampleID":      ["sampleid", "sample_id", "id", "labid", "ourlabid", "icode"],
    "SampleName":    ["samplename", "sample_name", "name", "description", "desc"],
    "Matrix":        ["matrix", "type", "material"],
    "Project":       ["project", "projectid", "project_name"],
    "Owner":         ["owner", "submitter", "client"],
    "ReceivedDate":  ["received", "receiveddate", "date_received", "recv_date"],
    "Remarks":       ["remarks", "comment", "comments", "note", "notes"],
    "ContainerType": ["containertype", "container_type", "container", "container_type"],
    "SampleVolume":  ["samplevolume", "sample_volume", "volume", "vol", "amount"],
}

ORG = "YourOrg"
APP = "ISOTOPES_SUITE"

class SampleManagementImportWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = QSettings(ORG, APP)
        self._raw_df: Optional[pd.DataFrame] = None
        self._mapped_df: Optional[pd.DataFrame] = None
        self._last_path: str = ""
        self._build_ui()
        self._restore_state()

    def _build_ui(self):
        v = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        v.addWidget(self.tabs, 1)

        # --- Import Tab ---
        self.pg_import = QWidget(self)
        iv = QVBoxLayout(self.pg_import)

        file_box = QGroupBox("Source file")
        fb = QHBoxLayout(file_box)
        self.path_edit = QLineEdit(file_box)
        self.path_edit.setPlaceholderText("CSV or XLSX...")
        btn_browse = QPushButton("Browse…", file_box)
        btn_browse.clicked.connect(self._pick_file)
        fb.addWidget(QLabel("File:"))
        fb.addWidget(self.path_edit, 1)
        fb.addWidget(btn_browse)
        iv.addWidget(file_box)

        opt_box = QGroupBox("Read options")
        ob = QHBoxLayout(opt_box)
        self.delim_edit = QLineEdit(opt_box); self.delim_edit.setMaxLength(1); self.delim_edit.setFixedWidth(40)
        self.delim_edit.setPlaceholderText(",")
        self.sheet_combo = QComboBox(opt_box); self.sheet_combo.setEditable(False)
        self.header_row_spin = QSpinBox(opt_box); self.header_row_spin.setRange(1, 1000); self.header_row_spin.setValue(1)
        ob.addWidget(QLabel("Delimiter (CSV):")); ob.addWidget(self.delim_edit)
        ob.addSpacing(16)
        ob.addWidget(QLabel("Sheet (XLSX):")); ob.addWidget(self.sheet_combo, 1)
        ob.addSpacing(16)
        ob.addWidget(QLabel("Header row:")); ob.addWidget(self.header_row_spin)
        iv.addWidget(opt_box)

        map_box = QGroupBox("Column mapping")
        mv = QFormLayout(map_box)
        self.map_combos: Dict[str, QComboBox] = {}
        self.keep_unmapped_check = QCheckBox("Keep unmapped columns in preview")
        for field in TARGET_FIELDS:
            cb = QComboBox(map_box); cb.setEditable(False)
            cb.addItem("— (ignore) —")
            self.map_combos[field] = cb
            mv.addRow(QLabel(field + ":"), cb)
        mv.addRow(self.keep_unmapped_check)
        iv.addWidget(map_box)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_load = QPushButton("Load")
        self.btn_load.clicked.connect(self._load_clicked)
        self.btn_automap = QPushButton("Auto-map")
        self.btn_automap.clicked.connect(self._automap_clicked)
        self.btn_preview = QPushButton("Build Preview")
        self.btn_preview.clicked.connect(self._build_preview_clicked)
        row.addWidget(self.btn_load); row.addWidget(self.btn_automap); row.addWidget(self.btn_preview)
        iv.addLayout(row)
        self.tabs.addTab(self.pg_import, "Import")

        # --- Preview Tab ---
        self.pg_preview = QWidget(self)
        pv = QVBoxLayout(self.pg_preview)
        self.preview_table = QTableWidget(self.pg_preview)
        pv.addWidget(self.preview_table, 1)
        self.tabs.addTab(self.pg_preview, "Preview")

        # --- Validate Tab ---
        self.pg_validate = QWidget(self)
        vv = QVBoxLayout(self.pg_validate)
        self.validate_btn = QPushButton("Run Validation", self.pg_validate)
        self.validate_btn.clicked.connect(self._run_validation)
        vv.addWidget(self.validate_btn, 0)
        self.validation_table = QTableWidget(self.pg_validate)
        vv.addWidget(self.validation_table, 1)
        self.tabs.addTab(self.pg_validate, "Validate")

        # --- Commit Tab ---
        self.pg_commit = QWidget(self)
        cv = QVBoxLayout(self.pg_commit)
        
        # REMOVED: DSN text input. We use global connection now.
        lbl_info = QLabel("Ready to register samples to the connected database.")
        lbl_info.setAlignment(Qt.AlignCenter)
        cv.addWidget(lbl_info)
        
        self.btn_commit = QPushButton("Register to LIMS", self.pg_commit)
        self.btn_commit.clicked.connect(self._commit_clicked)
        self.btn_commit.setEnabled(False)
        cv.addWidget(self.btn_commit, 0, Qt.AlignRight)
        cv.addStretch(1)
        self.tabs.addTab(self.pg_commit, "Commit")
    
    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose CSV/XLSX", self.settings.value("sample_mgmt/last_dir", ""),
            "Data Files (*.csv *.xlsx *.xls);;CSV (*.csv);;Excel (*.xlsx *.xls)"
        )
        if path:
            self.path_edit.setText(path)
            self._last_path = path
            try:
                self.settings.setValue("sample_mgmt/last_dir", os.path.dirname(path))
                self.settings.sync()
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            self._populate_sheets(path)

    def _populate_sheets(self, path: str):
        self.sheet_combo.clear()
        if path.lower().endswith((".xlsx", ".xls")):
            try:
                xl = pd.ExcelFile(path)
                for sh in xl.sheet_names: self.sheet_combo.addItem(sh)
            except Exception: self.sheet_combo.addItem("(active)")
        else:
            self.sheet_combo.addItem("(n/a)")

    def _load_clicked(self):
        path = self.path_edit.text().strip()
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "Import", "Please choose a valid file.")
            return
        header_row = max(1, int(self.header_row_spin.value()))
        header_idx = header_row - 1
        try:
            if path.lower().endswith(".csv"):
                delim = (self.delim_edit.text() or ",")
                df = pd.read_csv(path, sep=delim, header=header_idx, dtype=str, engine="python")
            else:
                sheet = self.sheet_combo.currentText() if self.sheet_combo.count() else None
                df = pd.read_excel(path, sheet_name=sheet if sheet and sheet != "(active)" else 0,
                                   header=header_idx, dtype=str)
        except Exception as e:
            QMessageBox.critical(self, "Import", f"Failed to read file:\n{e}")
            return
        df.columns = [str(c).strip() for c in df.columns]
        self._raw_df = df
        self._fill_mapping_choices(df.columns)
        QMessageBox.information(self, "Import", f"Loaded {len(df)} rows · {len(df.columns)} columns.")
        self._automap_clicked()

    def _fill_mapping_choices(self, headers: List[str]):
        for field, cb in self.map_combos.items():
            with _block_signals(cb):
                cb.clear()
                cb.addItem("— (ignore) —")
                for h in headers: cb.addItem(h)

    def _automap_clicked(self):
        if self._raw_df is None: return
        headers = [h.lower() for h in self._raw_df.columns]
        for field, cb in self.map_combos.items():
            best = self._find_best_header(field, headers)
            with _block_signals(cb):
                if best is not None:
                    idx = cb.findText(self._raw_df.columns[best])
                    if idx >= 0: cb.setCurrentIndex(idx)
                else:
                    cb.setCurrentIndex(0)

    def _find_best_header(self, target_field: str, headers_lower: List[str]) -> Optional[int]:
        if target_field.lower() in headers_lower: return headers_lower.index(target_field.lower())
        for alias in HEADER_ALIASES.get(target_field, []):
            if alias in headers_lower: return headers_lower.index(alias)
        return None

    def _build_preview_clicked(self):
        if self._raw_df is None:
            QMessageBox.warning(self, "Preview", "Load a file first.")
            return
        mapped = self._map_to_target(self._raw_df)
        if mapped is None: return
        mapped = self._normalize(mapped)
        self._mapped_df = mapped
        self._render_table(self.preview_table, mapped)
        self.tabs.setCurrentWidget(self.pg_preview)
        self.btn_commit.setEnabled(True)

    def _map_to_target(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        out = {}
        used_cols = set()
        for field, cb in self.map_combos.items():
            sel = cb.currentText().strip()
            if sel and not sel.startswith("—"):
                if sel not in df.columns:
                    QMessageBox.warning(self, "Mapping", f"Selected column '{sel}' not found in file.")
                    return None
                out[field] = df[sel].astype(str)
                used_cols.add(sel)
            else:
                out[field] = pd.Series([""] * len(df), index=df.index)
        mapped = pd.DataFrame(out)
        if self.keep_unmapped_check.isChecked():
            for col in df.columns:
                if col not in used_cols: mapped[col] = df[col]
        return mapped

    def _normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        x = df.copy()
        for c in x.columns: x[c] = x[c].astype(str).str.strip()
        if "Prefix" in x.columns: x["Prefix"] = x["Prefix"].str.upper()
        if "ReceivedDate" in x.columns:
            try: x["ReceivedDate"] = pd.to_datetime(x["ReceivedDate"], errors="coerce")
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        if all(k in x.columns for k in ("Prefix", "SampleID")):
            x["OurLabID"] = (x["Prefix"].astype(str).str.strip() + "-" +
                             x["SampleID"].astype(str).str.strip())
        return x

    def _run_validation(self):
        df = self._mapped_df
        if df is None or df.empty:
            QMessageBox.warning(self, "Validate", "Build a preview first.")
            return
        problems = []
        req = ["Prefix", "SampleID"]
        for r, row in df.iterrows():
            for f in req:
                if f not in df.columns or pd.isna(row.get(f, "")) or str(row.get(f, "")).strip() == "":
                    problems.append((r, f, "Required"))
        if all(c in df.columns for c in ("Prefix", "SampleID")):
            dup_mask = df.duplicated(subset=["Prefix", "SampleID"], keep=False)
            for r in df[dup_mask].index.tolist(): problems.append((r, "OurLabID", "Duplicate"))
        
        def bad_len(s, maxlen): 
            try: return len(str(s)) > maxlen
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); return False
        for r, row in df.iterrows():
            if bad_len(row.get("Prefix", ""), 10): problems.append((r, "Prefix", "Too long"))
            if bad_len(row.get("SampleID", ""), 50): problems.append((r, "SampleID", "Too long"))

        self._render_validation(problems, len(df))
        self.tabs.setCurrentWidget(self.pg_validate)
        if not problems: QMessageBox.information(self, "Validate", "No issues found. Ready to commit.")

    def _render_validation(self, problems: List[Tuple[int, str, str]], nrows: int):
        tbl = self.validation_table
        tbl.clear(); tbl.setColumnCount(3); tbl.setHorizontalHeaderLabels(["Row", "Field", "Issue"])
        tbl.setRowCount(len(problems) or 1)
        if not problems:
            tbl.setItem(0, 0, QTableWidgetItem("—"))
            tbl.setItem(0, 1, QTableWidgetItem("—"))
            tbl.setItem(0, 2, QTableWidgetItem("OK"))
        else:
            for i, (r, f, m) in enumerate(problems):
                tbl.setItem(i, 0, QTableWidgetItem(str(r + 2)))
                tbl.setItem(i, 1, QTableWidgetItem(f))
                tbl.setItem(i, 2, QTableWidgetItem(m))
        tbl.resizeColumnsToContents()

    def _commit_clicked(self):
        """
        Refactored to use db_manager.
        """
        if self._mapped_df is None or self._mapped_df.empty:
            QMessageBox.warning(self, "Commit", "Nothing to commit. Build a preview first.")
            return
        
        try:
            engine = db_manager.get_engine()
        except Exception:
            QMessageBox.critical(self, "Connection Error", "Database not connected. Please check Settings.")
            return

        # --- Resolve ContainerType names → IDs from lookup table ---
        container_name_to_id: Dict[str, int] = {}
        if "ContainerType" in self._mapped_df.columns:
            try:
                with db_manager.get_connection() as conn:
                    rows = conn.execute(text(
                        "SELECT container_type_id, type_name FROM public.container_type_lookup WHERE is_active"
                    )).fetchall()
                    for r in rows:
                        container_name_to_id[r[1].lower()] = r[0]
            except Exception as e:
                QMessageBox.warning(self, "Lookup", f"Could not load container type lookup:\n{e}")

        payload = self._mapped_df.copy()

        # Resolve ContainerType text → integer ID
        if "ContainerType" in payload.columns and container_name_to_id:
            payload["ContainerType"] = payload["ContainerType"].apply(
                lambda v: container_name_to_id.get(str(v).strip().lower()) if str(v).strip() else None
            )

        # Rename to match Sample table column names
        rename_map = {
            "SampleName":    "sName",
            "ReceivedDate":  "CollectionDate",
            "ContainerType": "container_type",
            "SampleVolume":  "SampleVolume",   # already correct
        }
        payload.rename(columns=rename_map, inplace=True)

        # Drop columns that don't map to Sample (Project, Owner are not standard Sample cols)
        db_cols = {"Prefix", "SampleID", "sName", "CollectionDate", "Remarks",
                   "container_type", "SampleVolume"}
        final_df = payload[[c for c in payload.columns if c in db_cols]]

        try:
            with engine.begin() as conn:
                final_df.to_sql("Sample", conn, if_exists="append", index=False)

            QMessageBox.information(self, "Commit", f"Registered {len(final_df)} samples to LIMS.")
        except Exception as e:
            QMessageBox.critical(self, "Commit", f"Failed to register samples:\n{e}")

    def _render_table(self, table: QTableWidget, df: pd.DataFrame):
        table.clear(); headers = [str(c) for c in df.columns]; table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers); table.setRowCount(len(df))
        for r, (_, row) in enumerate(df.iterrows()):
            for c, col in enumerate(headers):
                val = row.get(col, "")
                if isinstance(val, (pd.Timestamp, dt.datetime, dt.date)):
                    text = str(pd.to_datetime(val, errors="coerce"))[:19]
                else: text = str(val) if not pd.isna(val) else ""
                table.setItem(r, c, QTableWidgetItem(text))
        table.resizeColumnsToContents()

    def _restore_state(self):
        try:
            self.path_edit.setText(self.settings.value("sample_mgmt/import/path",""))
            self.delim_edit.setText(self.settings.value("sample_mgmt/import/delim", ","))
            self.header_row_spin.setValue(int(self.settings.value("sample_mgmt/import/header_row", 1)))
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    def _save_state(self):
        try:
            self.settings.setValue("sample_mgmt/import/path", self.path_edit.text().strip())
            self.settings.setValue("sample_mgmt/import/delim", self.delim_edit.text().strip() or ",")
            self.settings.setValue("sample_mgmt/import/header_row", self.header_row_spin.value())
            self.settings.sync()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

class _block_signals:
    def __init__(self, w): self.w = w
    def __enter__(self): 
        try: self.w.blockSignals(True)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
    def __exit__(self, *exc):
        try: self.w.blockSignals(False)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

def make_sample_mgmt_import_widget():
    return SampleManagementImportWidget()

class SampleImportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import New Samples")
        self.setMinimumSize(800, 600) # Set a reasonable default size
        
        main_layout = QVBoxLayout(self)
        
        # Create and add the core widget
        self.core_widget = SampleManagementImportWidget(self)
        main_layout.addWidget(self.core_widget, 1) # Give it stretch factor
        
        # Add a standard "Close" button
        # The "Commit" is handled inside the widget's own tab
        button_box = QDialogButtonBox(QDialogButtonBox.Close)
        button_box.rejected.connect(self.reject) # "Close" triggers reject
        
        main_layout.addWidget(button_box)
        
    def closeEvent(self, event):
        """
        Ensure settings are saved when the dialog is closed.
        """
        self.core_widget._save_state()
        super().closeEvent(event)


# --- This __main__ block demonstrates how to use BOTH ---
if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # --- How to use it as a Stand-Alone Dialog ---
    print("Opening as a stand-alone dialog...")
    
    dialog = SampleImportDialog()
    dialog.show() # Use show() instead of exec_() for a non-blocking demo
    
    # --- How the main launcher would use it (In-Canvas) ---
    # We create a simple QMainWindow to host the *widget*
    
    # print("\nOpening as an in-canvas widget...")
    # main_window = QMainWindow()
    # in_canvas_widget = SampleManagementImportWidget()
    # main_window.setCentralWidget(in_canvas_widget)
    # main_window.setWindowTitle("Main Launcher Showing In-Canvas Widget")
    # main_window.setGeometry(100, 100, 700, 500)
    # main_window.show()
    
    sys.exit(app.exec_())