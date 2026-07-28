"""
siam_preanalysis_runs_gui.py — Pre-analysis batch list panel for IsoWorks.
Provides PreAnalysisRunListWindow for listing, filtering, and opening
pre-isotope analysis (ChemistryBatch) runs, mirroring the SIAM run list layout.
"""
from __future__ import annotations
import logging

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QCheckBox, QComboBox,
    QLineEdit, QPushButton, QTableView, QLabel, QMessageBox, QHeaderView,
    QAbstractItemView, QDialog
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5.QtCore import Qt, QCoreApplication

from db_core import db_manager
from sqlalchemy import text
from shared_utils import check_employee_privilege, get_current_user_id, normalize_login_name


class PreAnalysisRunListWindow(QWidget):
    """
    List, filter and open Pre-Isotope Analysis Batches (ChemistryBatch).
    Mirrors SiamRunListWindow layout conventions.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PreAnalysisRunList")

        self.has_write_priv = False
        self.has_admin_priv = False

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ── Top action bar (right-aligned, matching SiamRunListWindow) ────
        root.addLayout(self._build_action_bar())
        root.addSpacing(10)

        # ── Filter / Search group ─────────────────────────────────────────
        root.addWidget(self._build_filter_group())
        root.addSpacing(10)

        # ── Run table ─────────────────────────────────────────────────────
        self.model = QStandardItemModel()
        self.model.setHorizontalHeaderLabels(
            ["Run ID", "Procedure", "Instrument", "Start Date", "End Date", "Samples", "Status"]
        )

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.setStyleSheet("""
            QTableView { border: none; background-color: white; }
            QTableView::item { padding: 6px 8px; border: none; background-color: white; color: #333; }
            QTableView::item:alternate { background-color: #F3F7FA; }
            QTableView::item:selected { background-color: #DDEEFF; color: #000; }
            QHeaderView::section { background-color: white; color: #7F8BB5; font-weight: bold;
                                   padding: 6px 8px; border: none; border-bottom: 2px solid #7F8BB5; }
            QHeaderView::section:hover { background-color: #DDEEFF; color: #000; }
        """)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)   # Procedure stretches
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.clicked.connect(self._on_table_clicked)
        root.addWidget(self.table, 1)

        # ── Status bar ────────────────────────────────────────────────────
        self.lblStatus = QLabel("")
        root.addWidget(self.lblStatus)

        # ── Privilege + startup ───────────────────────────────────────────
        self._check_privileges()
        self._update_ui_state()

        try:
            db_manager.get_engine()
            self.setEnabled(True)
            self.load_runs()
        except Exception:
            self.setEnabled(False)
            lbl = QLabel("<h2>No Database Connection</h2><p>Please configure in Settings.</p>")
            lbl.setAlignment(Qt.AlignCenter)
            root.addWidget(lbl)

    # ──────────────────────────────────────────────────────────────────────
    # Layout builders
    # ──────────────────────────────────────────────────────────────────────

    def _build_action_bar(self) -> QHBoxLayout:
        """Top bar: [stretch] [New Batch] [Refresh] [Close]"""
        lay = QHBoxLayout()

        self.btnNew = QPushButton("New Batch")
        self.btnNew.setStyleSheet("""
            QPushButton { background-color: #27ae60; color: white; font-weight: bold;
                          border: none; padding: 5px 14px; border-radius: 4px; }
            QPushButton:hover    { background-color: #219a52; }
            QPushButton:pressed  { background-color: #1e8449; }
            QPushButton:disabled { background-color: #a9dfbf; color: #7f8c8d; }
        """)
        self.btnRefresh = QPushButton("Refresh")
        self.btnClose   = QPushButton("Close")

        lay.addStretch(1)
        lay.addWidget(self.btnNew)
        lay.addWidget(self.btnRefresh)
        lay.addWidget(self.btnClose)

        self.btnNew.clicked.connect(self._create_new_batch)
        self.btnRefresh.clicked.connect(self.load_runs)
        self.btnClose.clicked.connect(self._close_module)

        return lay

    def _build_filter_group(self) -> QGroupBox:
        """Filter / Search row with merged Search+Open button and Delete."""
        grp = QGroupBox("Filter / Search Pre-Analysis Batches")
        lay = QVBoxLayout(grp)

        # Row 1 — simple checkbox filter
        row1 = QHBoxLayout()
        self.chkOpenOnly = QCheckBox("Open batches only")
        self.chkOpenOnly.setChecked(True)
        row1.addWidget(self.chkOpenOnly)
        row1.addStretch(1)
        lay.addLayout(row1)

        # Row 2 — search / open / delete
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Search By:"))

        self.cmbSearchType = QComboBox()
        self.cmbSearchType.addItems(["Run ID", "Sample Name"])
        row2.addWidget(self.cmbSearchType)

        self.txtSearch = QLineEdit()
        self.txtSearch.setPlaceholderText("Enter search term…")
        self.txtSearch.returnPressed.connect(self._on_search_or_open)
        row2.addWidget(self.txtSearch, 1)

        # Merged Search / Open Batch button (label changes with search type / selection)
        self.btnSearch = QPushButton("Open Batch")
        self.btnSearch.clicked.connect(self._on_search_or_open)
        row2.addWidget(self.btnSearch)

        self.btnDelete = QPushButton("Delete")
        self.btnDelete.setStyleSheet("""
            QPushButton { background-color: #c0392b; color: white; font-weight: bold;
                          border: none; padding: 5px 14px; border-radius: 4px; }
            QPushButton:hover    { background-color: #a93226; }
            QPushButton:pressed  { background-color: #922b21; }
            QPushButton:disabled { background-color: #f1948a; color: #7f8c8d; }
        """)
        self.btnDelete.clicked.connect(self._delete_selected)
        row2.addWidget(self.btnDelete)

        lay.addLayout(row2)

        # Wire filter signals
        self.chkOpenOnly.toggled.connect(self.load_runs)
        self.cmbSearchType.currentTextChanged.connect(self._on_search_type_changed)

        return grp

    # ──────────────────────────────────────────────────────────────────────
    # Privileges
    # ──────────────────────────────────────────────────────────────────────

    def _check_privileges(self):
        try:
            user = normalize_login_name(get_current_user_id())
            self.has_write_priv = check_employee_privilege(user, "accesssiam")
            self.has_admin_priv = check_employee_privilege(user, "superadmin")
            logging.info("PreAnalysis privileges — user=%s write=%s admin=%s",
                         user, self.has_write_priv, self.has_admin_priv)
        except Exception as exc:
            logging.error("PreAnalysis _check_privileges: %s", exc)
            self.has_write_priv = False
            self.has_admin_priv = False

    def _update_ui_state(self):
        self.btnNew.setEnabled(self.has_write_priv)
        self.btnDelete.setEnabled(self.has_admin_priv)

    # ──────────────────────────────────────────────────────────────────────
    # Data loading
    # ──────────────────────────────────────────────────────────────────────

    def load_runs(self):
        self.model.removeRows(0, self.model.rowCount())
        open_only = self.chkOpenOnly.isChecked()
        search    = self.txtSearch.text().strip()
        stype     = self.cmbSearchType.currentText()

        try:
            with db_manager.get_connection() as conn:
                sql = """
                    SELECT cb.runid,
                           ap.procedurename,
                           COALESCE(eq.equipmentname, '') AS instrument,
                           cb.runstarttime,
                           cb.runendtime,
                           COUNT(cl.analysisid)           AS samples,
                           CASE WHEN cb.runendtime IS NOT NULL THEN 'Finalized' ELSE 'Open' END AS status
                    FROM   chem.batch cb
                    JOIN   public.analysisprocedure ap ON ap.procedureid = cb.procedureid
                    LEFT JOIN public.equipment       eq ON eq.equipmentid = cb.equipmentid
                    LEFT JOIN chem.loadlist          cl ON cl.runid = cb.runid
                """
                where_parts: list[str] = []
                params: dict = {}

                if open_only:
                    where_parts.append("cb.runendtime IS NULL")

                if search:
                    if stype == "Run ID" and search.isdigit():
                        where_parts.append("cb.runid = :rid")
                        params["rid"] = int(search)
                    elif stype == "Sample Name":
                        where_parts.append("""cb.runid IN (
                            SELECT cl2.runid FROM chem.loadlist cl2
                            JOIN public.analysis a2 ON a2.analysisid = cl2.analysisid
                            JOIN public.sample   s2 ON s2.sampleid = a2.sampleid AND s2.prefix = a2.prefix
                            WHERE s2.sname ILIKE :sname
                        )""")
                        params["sname"] = f"%{search}%"

                if where_parts:
                    sql += " WHERE " + " AND ".join(where_parts)

                sql += """
                    GROUP BY cb.runid, ap.procedurename, eq.equipmentname,
                             cb.runstarttime, cb.runendtime
                    ORDER BY cb.runid DESC
                """
                rows = conn.execute(text(sql), params).fetchall()

            for row in rows:
                run_id, proc, instr, start, end, n_samp, status = row
                start_str = start.strftime("%Y-%m-%d %H:%M") if hasattr(start, "strftime") else str(start or "")
                end_str   = end.strftime("%Y-%m-%d %H:%M")   if hasattr(end,   "strftime") else ("—" if not end else str(end))

                items = [
                    QStandardItem(str(run_id)),
                    QStandardItem(proc    or ""),
                    QStandardItem(instr   or ""),
                    QStandardItem(start_str),
                    QStandardItem(end_str),
                    QStandardItem(str(n_samp or 0)),
                    QStandardItem(status),
                ]
                items[0].setData(run_id, Qt.UserRole)

                color = Qt.darkGreen if status == "Finalized" else Qt.darkBlue
                for it in items:
                    it.setForeground(color)

                self.model.appendRow(items)

            self.lblStatus.setText(f"{len(rows)} batch(es) loaded.")

        except Exception as exc:
            logging.error("PreAnalysisRunList.load_runs: %s", exc)
            self.lblStatus.setText(f"Error: {exc}")

    def refresh(self):
        """Standard interface for launcher."""
        self.load_runs()

    # ──────────────────────────────────────────────────────────────────────
    # Search / table interaction
    # ──────────────────────────────────────────────────────────────────────

    def _on_search_type_changed(self, text: str):
        self.btnSearch.setText("Open Batch" if text == "Run ID" else "Search")

    def _on_table_clicked(self, index):
        """Fill Run ID into the search box and switch to 'Open Batch' mode."""
        if not index.isValid():
            return
        item = self.model.item(index.row(), 0)
        if item:
            rid = item.data(Qt.UserRole)
            if rid is not None:
                self.cmbSearchType.setCurrentText("Run ID")
                self.txtSearch.setText(str(rid))

    def _on_search_or_open(self):
        if self.cmbSearchType.currentText() == "Run ID":
            val = self.txtSearch.text().strip()
            if not val:
                self.load_runs()
                return
            try:
                run_id = int(val)
            except ValueError:
                QMessageBox.warning(self, "Invalid ID", "Run ID must be a number.")
                return
            self._open_details(run_id)
        else:
            self.load_runs()

    def _on_double_click(self, _index):
        run_id = self._selected_run_id()
        if run_id is not None:
            self._open_details(run_id)

    # ──────────────────────────────────────────────────────────────────────
    # Actions
    # ──────────────────────────────────────────────────────────────────────

    def _selected_run_id(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.model.item(rows[0].row(), 0).data(Qt.UserRole)

    def _open_details(self, run_id: int):
        try:
            from siam_preanalysis_details_gui import PreAnalysisDetailsWindow
        except ImportError as exc:
            QMessageBox.critical(self, "Error", f"Cannot open details:\n{exc}")
            return

        win = PreAnalysisDetailsWindow(run_id=run_id, parent=self)
        win.setWindowFlags(Qt.Window)
        win.setWindowTitle(f"Pre-Analysis Batch: {run_id}")
        win.resize(950, 600)
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.destroyed.connect(lambda: self._on_details_closed(0))
        win.show()

    def _on_details_closed(self, _result):
        self._reset_filters()

    def _reset_filters(self):
        self.chkOpenOnly.setChecked(True)
        self.cmbSearchType.setCurrentIndex(0)
        self.txtSearch.clear()
        self.load_runs()

    def _create_new_batch(self):
        try:
            from siam_preanalysis_create_gui import PreAnalysisCreateDialog
        except ImportError as exc:
            QMessageBox.critical(self, "Error", f"Cannot open create dialog:\n{exc}")
            return

        dlg = PreAnalysisCreateDialog(parent=self)
        if dlg.exec_() == QDialog.Accepted:
            self.load_runs()
            new_id = getattr(dlg, "created_run_id", None)
            if new_id:
                self._open_details(new_id)

    def _delete_selected(self):
        run_id = self._selected_run_id()
        if run_id is None:
            QMessageBox.information(self, "Select", "Please select a batch first.")
            return

        reply = QMessageBox.question(
            self, "Delete Batch",
            f"Delete Pre-Analysis Batch {run_id}?\n"
            "All load list entries and related Analysis records will be removed.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        try:
            with db_manager.get_connection() as conn:
                aids = [r[0] for r in conn.execute(
                    text("SELECT analysisid FROM chem.loadlist WHERE runid = :rid"),
                    {"rid": run_id}
                ).fetchall()]
                conn.execute(text("DELETE FROM chem.loadlist WHERE runid = :rid"), {"rid": run_id})
                conn.execute(text("DELETE FROM chem.batch    WHERE runid = :rid"), {"rid": run_id})
                for aid in aids:
                    conn.execute(text("DELETE FROM Analysis WHERE AnalysisID = :aid"), {"aid": aid})
                conn.commit()

            QMessageBox.information(self, "Deleted", f"Batch {run_id} deleted.")
            self.load_runs()
        except Exception as exc:
            logging.error("Delete batch failed: %s", exc)
            QMessageBox.critical(self, "Error", f"Delete failed:\n{exc}")

    def _close_module(self):
        if isinstance(self.parent(), QWidget):
            self.close()
        else:
            QCoreApplication.instance().quit()
