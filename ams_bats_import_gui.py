"""
ams_bats_import_gui.py — Batch import of BATS / HVEE Tandetron processed files.

Workflow
────────
1. Add one or more BATS output files (.dat/.txt/.ams/.csv).
2. "Parse & Preview" parses every file and looks up its run code in the DB.
3. Review the preview table (existing run, new run, duplicate-wheel warnings).
4. "Import All" inserts amsrun (if new) → amswheel → amstarget → amsmeasurement
   → amsresult for every file shown as Ready.
5. Optional: auto-reduce immediately after import.

Entry point: BATSImportDialog(parent=None)
"""
from __future__ import annotations
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QTableView, QAbstractItemView, QHeaderView,
    QFileDialog, QMessageBox, QCheckBox, QTextEdit, QSplitter,
    QWidget, QListWidget, QListWidgetItem, QProgressBar,
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QColor, QBrush, QFont
from PyQt5.QtCore import Qt, QThread, pyqtSignal

from db_core import db_manager
from sqlalchemy import text
from shared_utils import get_current_user_id, normalize_login_name
from gui_utils import show_message
from ams_parser import HVEETandetronParser, AMSWheelRecord, import_wheel_to_db

log = logging.getLogger(__name__)

# ── Preview table column indices ──────────────────────────────────────────────
P_IDX      = 0
P_FILE     = 1
P_RUNCODE  = 2
P_DATE     = 3
P_WHEEL    = 4
P_TARGETS  = 5
P_UNKNOWN  = 6
P_STDS     = 7
P_BLANKS   = 8
P_DB       = 9    # DB match status string
P_STATUS   = 10   # import status

_HEADERS = [
    "#", "File", "Run Code", "Date", "Wheel Label",
    "Targets", "Unknowns", "Stds", "Blanks",
    "DB Match", "Status",
]

# Status constants
ST_PENDING  = "Pending"
ST_READY_EX = "Ready — existing run"   # run code found in DB
ST_READY_NW = "Ready — new run"        # run code not in DB
ST_DUPE     = "Warning — wheel exists" # wheel label already on this run
ST_ERROR    = "Parse error"
ST_IMPORTED = "Imported ✓"
ST_FAILED   = "Failed"

_ST_COLORS = {
    ST_PENDING:  QColor("#FFF9C4"),   # pale yellow
    ST_READY_EX: QColor("#E8F5E9"),   # light green
    ST_READY_NW: QColor("#E3F2FD"),   # light blue
    ST_DUPE:     QColor("#FFF3E0"),   # amber
    ST_ERROR:    QColor("#FFEBEE"),   # pink-red
    ST_IMPORTED: QColor("#C8E6C9"),   # green
    ST_FAILED:   QColor("#FFCDD2"),   # red
}

_TABLE_SS = (
    "QTableView { border:1px solid #b3d4f5; }"
    "QTableView::item { padding:4px 6px; }"
    "QTableView::item:selected { background:#DDEEFF; color:#000; }"
    "QHeaderView::section { background:#e8f4fd; color:#37474f; font-weight:600;"
    " padding:4px 6px; border:none; border-bottom:2px solid #90caf9; }"
)


# ── Worker thread ─────────────────────────────────────────────────────────────

class _ImportWorker(QThread):
    """
    Runs the full import sequence in a background thread so the UI stays
    responsive.  Emits progress messages and per-file results.
    """
    log_line   = pyqtSignal(str)          # plain text for the log widget
    row_done   = pyqtSignal(int, str, str)  # (preview_row, status_str, db_info)
    all_done   = pyqtSignal(int, int)     # (n_ok, n_fail)

    def __init__(self, jobs: list[dict], auto_reduce: bool, user: str):
        super().__init__()
        self._jobs        = jobs          # list of {row, path, wheel, run_id(opt), is_new}
        self._auto_reduce = auto_reduce
        self._user        = user

    def run(self):
        n_ok = n_fail = 0
        for job in self._jobs:
            row    = job["row"]
            path   = job["path"]
            wheel  = job["wheel"]
            run_id = job.get("run_id")
            is_new = job.get("is_new", False)

            self.log_line.emit(f"▶ {Path(path).name}  [{wheel.run_code}]")

            try:
                # ── 1. Create run if new ───────────────────────────────────
                if is_new or run_id is None:
                    run_id = self._create_run(wheel)
                    self.log_line.emit(f"  Created run {run_id} ({wheel.run_code})")

                # ── 2. Next wheel number ───────────────────────────────────
                with db_manager.get_connection() as conn:
                    wn = conn.execute(text(
                        "SELECT COALESCE(MAX(wheelnumber),0)+1 FROM ams.amswheel "
                        "WHERE amsrunid=:rid"
                    ), {"rid": run_id}).scalar()

                # ── 3. Import wheel ────────────────────────────────────────
                wheel_id = import_wheel_to_db(wheel, run_id=run_id, wheel_number=wn)
                self.log_line.emit(
                    f"  Wheel {wn} imported → amswheelid={wheel_id}  "
                    f"({len(wheel.targets)} targets)"
                )

                # ── 4. Auto-reduce ─────────────────────────────────────────
                if self._auto_reduce and wheel.contains_reduced_data:
                    self.log_line.emit(
                        "  ⚠ Auto-reduce skipped — file already contains "
                        "reduced data (Fm/d13C); reduction would double-reduce."
                    )
                elif self._auto_reduce:
                    self.log_line.emit("  Reducing…")
                    try:
                        from ams_reduction import reduce_run
                        result = reduce_run(run_id, user=self._user)
                        n_tgts = sum(len(wr.targets) for wr in result.wheels)
                        self.log_line.emit(f"  Reduction complete — {n_tgts} targets")
                        for wr in result.wheels:
                            for w in wr.warnings:
                                self.log_line.emit(f"  ⚠ {w}")
                    except Exception as re_exc:
                        self.log_line.emit(f"  ⚠ Reduction failed: {re_exc}")

                self.row_done.emit(row, ST_IMPORTED, f"Run {run_id}")
                n_ok += 1

            except Exception as exc:
                log.error("BATS import %s: %s", path, exc, exc_info=True)
                self.log_line.emit(f"  ✗ {exc}")
                self.row_done.emit(row, ST_FAILED, str(exc))
                n_fail += 1

        self.all_done.emit(n_ok, n_fail)

    def _create_run(self, wheel: AMSWheelRecord) -> int:
        equip_id = self._match_equipment(wheel.machine)
        tech_id  = self._match_employee(wheel.operator)
        now      = datetime.now()
        with db_manager.get_connection() as conn:
            row = conn.execute(text("""
                INSERT INTO ams.amsrun
                    (runcode, rundate, equipmentid, technicianid,
                     notes, runstatus, islocked,
                     createdatestamp, createuserstamp)
                VALUES
                    (:code, :dt, :eid, :tid,
                     :notes, 0, FALSE, :now, :user)
                RETURNING amsrunid
            """), {
                "code":  wheel.run_code,
                "dt":    wheel.run_date,
                "eid":   equip_id,
                "tid":   tech_id,
                "notes": f"Auto-created by BATS import. Machine: {wheel.machine}",
                "now":   now,
                "user":  self._user,
            }).fetchone()
            conn.commit()
        return row[0]

    @staticmethod
    def _match_equipment(machine_name: str) -> Optional[int]:
        if not machine_name:
            return None
        try:
            with db_manager.get_connection() as conn:
                r = conn.execute(text(
                    "SELECT EquipmentID FROM Equipment "
                    "WHERE EquipmentName ILIKE :n "
                    "AND IsObsolete IS NOT TRUE LIMIT 1"
                ), {"n": f"%{machine_name}%"}).fetchone()
            return r[0] if r else None
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return None

    @staticmethod
    def _match_employee(operator: str) -> Optional[int]:
        if not operator:
            return None
        try:
            parts = operator.strip().split()
            last  = parts[-1] if parts else operator
            with db_manager.get_connection() as conn:
                r = conn.execute(text(
                    "SELECT EmployeeID FROM Employee "
                    "WHERE LastName ILIKE :n LIMIT 1"
                ), {"n": f"%{last}%"}).fetchone()
            return r[0] if r else None
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return None


# ── Main dialog ───────────────────────────────────────────────────────────────

class BATSImportDialog(QDialog):
    """
    Batch BATS file importer.

    Top    — file list (add/remove) + action buttons
    Middle — parse preview table
    Bottom — options + scrollable import log
    """

    # Emitted when at least one run was imported (so callers can refresh)
    import_completed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import Processed AMS Data (BATS)")
        self.setModal(True)
        self.resize(1100, 780)

        self._parsed: dict[str, AMSWheelRecord] = {}  # path → wheel
        self._run_id_cache: dict[str, Optional[int]] = {}  # run_code → run_id | None
        self._worker: Optional[_ImportWorker] = None

        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        root.addWidget(self._build_file_section())

        # Preview table
        preview_grp = QGroupBox("Parse Preview")
        pvlay = QVBoxLayout(preview_grp)

        self._preview_model = QStandardItemModel()
        self._preview_model.setHorizontalHeaderLabels(_HEADERS)
        self._preview_table = QTableView()
        self._preview_table.setModel(self._preview_model)
        self._preview_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._preview_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._preview_table.setAlternatingRowColors(False)
        self._preview_table.verticalHeader().setVisible(False)
        self._preview_table.setStyleSheet(_TABLE_SS)
        pvlay.addWidget(self._preview_table)
        root.addWidget(preview_grp, 1)

        # Options + log
        root.addWidget(self._build_options_section())
        root.addWidget(self._build_log_section())

        root.addWidget(self._build_action_bar())

    def _build_file_section(self) -> QGroupBox:
        grp = QGroupBox("BATS Data Files")
        lay = QHBoxLayout(grp)

        self._file_list = QListWidget()
        self._file_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._file_list.setMaximumHeight(100)
        lay.addWidget(self._file_list, 1)

        btn_col = QVBoxLayout()
        btnAdd    = QPushButton("Add Files…")
        btnRemove = QPushButton("Remove")
        btnClear  = QPushButton("Clear All")

        btnAdd.setStyleSheet(
            "QPushButton { background:#1976d2; color:white; font-weight:bold;"
            " border:none; padding:4px 10px; border-radius:4px; }"
            "QPushButton:hover { background:#1565c0; }"
        )
        btnAdd.clicked.connect(self._add_files)
        btnRemove.clicked.connect(self._remove_selected_files)
        btnClear.clicked.connect(self._clear_files)

        btn_col.addWidget(btnAdd)
        btn_col.addWidget(btnRemove)
        btn_col.addWidget(btnClear)
        btn_col.addStretch()
        lay.addLayout(btn_col)
        return grp

    def _build_options_section(self) -> QGroupBox:
        grp = QGroupBox("Import Options")
        lay = QHBoxLayout(grp)
        self.chkAutoReduce = QCheckBox("Auto-reduce after import")
        self.chkAutoReduce.setChecked(True)
        self.chkOpenDetails = QCheckBox("Open run details after single-file import")
        lay.addWidget(self.chkAutoReduce)
        lay.addSpacing(24)
        lay.addWidget(self.chkOpenDetails)
        lay.addStretch()
        return grp

    def _build_log_section(self) -> QGroupBox:
        grp = QGroupBox("Import Log")
        lay = QVBoxLayout(grp)
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(130)
        self._log_box.setFont(QFont("Courier New", 9))
        lay.addWidget(self._log_box)
        return grp

    def _build_action_bar(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(6)
        lay.addWidget(self._progress, 1)

        self.btnParse = QPushButton("Parse & Preview")
        self.btnParse.setStyleSheet(
            "QPushButton { background:#546e7a; color:white; font-weight:bold;"
            " border:none; padding:5px 14px; border-radius:4px; }"
            "QPushButton:hover { background:#455a64; }"
        )
        self.btnParse.clicked.connect(self._parse_files)

        self.btnImport = QPushButton("Import All")
        self.btnImport.setEnabled(False)
        self.btnImport.setStyleSheet(
            "QPushButton { background:#27ae60; color:white; font-weight:bold;"
            " border:none; padding:5px 14px; border-radius:4px; }"
            "QPushButton:hover { background:#219a52; }"
            "QPushButton:disabled { background:#a9dfbf; color:#7f8c8d; }"
        )
        self.btnImport.clicked.connect(self._start_import)

        btnClose = QPushButton("Close")
        btnClose.clicked.connect(self.accept)

        for b in [self.btnParse, self.btnImport, btnClose]:
            lay.addWidget(b)
        return w

    # ── File management ───────────────────────────────────────────────────────

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select BATS Data Files",
            "", "Data files (*.dat *.txt *.ams *.csv);;All files (*)"
        )
        existing = {self._file_list.item(i).data(Qt.UserRole)
                    for i in range(self._file_list.count())}
        for p in paths:
            if p not in existing:
                item = QListWidgetItem(Path(p).name)
                item.setData(Qt.UserRole, p)
                item.setToolTip(p)
                self._file_list.addItem(item)

    def _remove_selected_files(self):
        for item in self._file_list.selectedItems():
            self._file_list.takeItem(self._file_list.row(item))

    def _clear_files(self):
        self._file_list.clear()
        self._preview_model.removeRows(0, self._preview_model.rowCount())
        self._parsed.clear()
        self._run_id_cache.clear()
        self.btnImport.setEnabled(False)

    def _all_file_paths(self) -> list[str]:
        return [
            self._file_list.item(i).data(Qt.UserRole)
            for i in range(self._file_list.count())
        ]

    # ── Parse & preview ───────────────────────────────────────────────────────

    def _parse_files(self):
        paths = self._all_file_paths()
        if not paths:
            show_message(self, "No Files", "Add at least one BATS file first.")
            return

        self._preview_model.removeRows(0, self._preview_model.rowCount())
        self._parsed.clear()
        self._run_id_cache.clear()
        self._log("Parsing files…")

        parser = HVEETandetronParser()
        ready_count = 0

        for idx, path in enumerate(paths):
            fname = Path(path).name

            try:
                wheel = parser.parse(path)
                self._parsed[path] = wheel
            except Exception as exc:
                log.warning("Parse failed %s: %s", fname, exc)
                self._add_preview_row(
                    idx + 1, fname, "—", "—", "—", 0, 0, 0, 0,
                    "—", ST_ERROR, str(exc),
                )
                self._log(f"  ✗ {fname}: {exc}")
                continue

            # DB lookup
            run_id, db_status = self._lookup_run(wheel.run_code, wheel.wheel_label)
            self._run_id_cache[wheel.run_code] = run_id

            n_unk   = sum(1 for t in wheel.targets if t.target_type == "unknown")
            n_std   = sum(1 for t in wheel.targets if t.target_type in ("OXI", "OXII"))
            n_blank = sum(1 for t in wheel.targets if "blank" in t.target_type)

            if db_status == "dupe":
                st = ST_DUPE
            elif run_id is not None:
                st = ST_READY_EX
            else:
                st = ST_READY_NW

            if st in (ST_READY_EX, ST_READY_NW):
                ready_count += 1

            self._add_preview_row(
                idx + 1, fname,
                wheel.run_code,
                str(wheel.run_date) if wheel.run_date else "—",
                wheel.wheel_label,
                len(wheel.targets), n_unk, n_std, n_blank,
                (f"Run {run_id}" if run_id else "New run"),
                st,
            )
            self._log(f"  ✓ {fname}  [{wheel.run_code}  {db_status}]")

        self._apply_preview_widths()
        self.btnImport.setEnabled(ready_count > 0)
        self._log(f"Done — {ready_count} file(s) ready to import.")

    def _lookup_run(self, run_code: str, wheel_label: str) -> tuple[Optional[int], str]:
        """Return (run_id | None, status_hint).
        status_hint: 'found' | 'new' | 'dupe'
        """
        try:
            with db_manager.get_connection() as conn:
                r = conn.execute(text(
                    "SELECT amsrunid FROM ams.amsrun WHERE runcode = :c"
                ), {"c": run_code}).fetchone()

                if r is None:
                    return None, "new"

                run_id = r[0]
                # Check for duplicate wheel label
                dupe = conn.execute(text(
                    "SELECT 1 FROM ams.amswheel "
                    "WHERE amsrunid=:rid AND wheellabel=:wlbl LIMIT 1"
                ), {"rid": run_id, "wlbl": wheel_label}).fetchone()
                return run_id, ("dupe" if dupe else "found")
        except Exception as exc:
            log.warning("DB lookup run_code=%s: %s", run_code, exc)
            return None, "new"

    def _add_preview_row(
        self, idx, fname, run_code, date, wheel_lbl,
        n_tgt, n_unk, n_std, n_blank, db_info, status, tooltip=""
    ):
        color = QBrush(_ST_COLORS.get(status, QColor("#FFFFFF")))
        cells = [
            QStandardItem(str(idx)),
            QStandardItem(fname),
            QStandardItem(run_code),
            QStandardItem(date),
            QStandardItem(wheel_lbl),
            QStandardItem(str(n_tgt)),
            QStandardItem(str(n_unk)),
            QStandardItem(str(n_std)),
            QStandardItem(str(n_blank)),
            QStandardItem(db_info),
            QStandardItem(status),
        ]
        for it in cells:
            it.setBackground(color)
            if tooltip:
                it.setToolTip(tooltip)
        self._preview_model.appendRow(cells)

    def _apply_preview_widths(self):
        h = self._preview_table.horizontalHeader()
        h.setSectionResizeMode(P_IDX,     QHeaderView.Fixed)
        self._preview_table.setColumnWidth(P_IDX, 30)
        h.setSectionResizeMode(P_FILE,    QHeaderView.ResizeToContents)
        h.setSectionResizeMode(P_RUNCODE, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(P_DATE,    QHeaderView.ResizeToContents)
        h.setSectionResizeMode(P_WHEEL,   QHeaderView.ResizeToContents)
        for col in (P_TARGETS, P_UNKNOWN, P_STDS, P_BLANKS):
            h.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(P_DB,     QHeaderView.ResizeToContents)
        h.setSectionResizeMode(P_STATUS, QHeaderView.Stretch)

    # ── Import ────────────────────────────────────────────────────────────────

    def _start_import(self):
        # Collect ready jobs from preview table
        jobs: list[dict] = []
        paths = self._all_file_paths()

        for row in range(self._preview_model.rowCount()):
            status = self._preview_model.item(row, P_STATUS).text()
            if status not in (ST_READY_EX, ST_READY_NW, ST_DUPE):
                continue

            # Match row to file by index (1-based in column P_IDX)
            file_idx = int(self._preview_model.item(row, P_IDX).text()) - 1
            if file_idx >= len(paths):
                continue
            path  = paths[file_idx]
            wheel = self._parsed.get(path)
            if wheel is None:
                continue

            run_code = wheel.run_code
            run_id   = self._run_id_cache.get(run_code)
            is_new   = run_id is None

            if status == ST_DUPE:
                ans = QMessageBox.question(
                    self, "Duplicate Wheel",
                    f"File '{Path(path).name}':\n"
                    f"Wheel label '{wheel.wheel_label}' already exists in run {run_id}.\n\n"
                    "Import as an additional wheel anyway?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if ans == QMessageBox.No:
                    continue

            jobs.append({
                "row": row, "path": path, "wheel": wheel,
                "run_id": run_id, "is_new": is_new,
            })

        if not jobs:
            show_message(self, "Nothing to Import",
                         "No files in a Ready state. Parse files first.")
            return

        user = normalize_login_name(get_current_user_id())
        self._set_buttons_enabled(False)
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)   # indeterminate

        self._worker = _ImportWorker(
            jobs        = jobs,
            auto_reduce = self.chkAutoReduce.isChecked(),
            user        = user,
        )
        self._worker.log_line.connect(self._log)
        self._worker.row_done.connect(self._on_row_done)
        self._worker.all_done.connect(self._on_all_done)
        self._worker.start()

    def _on_row_done(self, row: int, status: str, db_info: str):
        color = QBrush(_ST_COLORS.get(status, QColor("#FFFFFF")))
        for col in range(self._preview_model.columnCount()):
            it = self._preview_model.item(row, col)
            if it:
                it.setBackground(color)
        st_it = self._preview_model.item(row, P_STATUS)
        if st_it:
            st_it.setText(status)
        db_it = self._preview_model.item(row, P_DB)
        if db_it and db_info:
            db_it.setText(db_info)

    def _on_all_done(self, n_ok: int, n_fail: int):
        self._progress.setVisible(False)
        self._set_buttons_enabled(True)
        self._log(f"\n{'─'*50}")
        self._log(f"Complete — {n_ok} imported, {n_fail} failed.")

        if n_ok > 0:
            self.import_completed.emit()

        # Auto-open run details for single-file import
        if (n_ok == 1 and n_fail == 0
                and self.chkOpenDetails.isChecked()):
            self._try_open_run_details()

    def _try_open_run_details(self):
        """Open AMSRunDetailsWindow for the single imported run."""
        # Find the imported row's run_id from DB info column
        for row in range(self._preview_model.rowCount()):
            st = self._preview_model.item(row, P_STATUS).text()
            if st == ST_IMPORTED:
                db_txt = self._preview_model.item(row, P_DB).text()
                # db_txt is "Run {run_id}"
                try:
                    run_id = int(db_txt.split()[-1])
                    try:
                        from ams_run_details_gui import AMSRunDetailsWindow
                        win = AMSRunDetailsWindow(run_id=run_id, parent=self.parent())
                        win.show()
                    except ImportError:
                        pass
                except ValueError:
                    pass
                break

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _log(self, msg: str):
        self._log_box.append(msg)
        self._log_box.verticalScrollBar().setValue(
            self._log_box.verticalScrollBar().maximum()
        )

    def _set_buttons_enabled(self, enabled: bool):
        self.btnParse.setEnabled(enabled)
        self.btnImport.setEnabled(enabled)
