"""
ngam_extraction_runs_gui.py
===========================
NGAM – Noble Gas Extraction Run List

Mirrors ngam_ingrowth_runs_gui.py pattern for NGExtractionRun / NGExtractionData.

Schema (ngam.ngextractionrun):
    RunID (plain integer, MAX+1), ProcedureID, EquipmentID, WorkflowID,
    IsLocked, RunStatus (4=pending, 5=complete),
    RunStartTime, RunEndTime, TechnicianID, Remarks,
    CreateDateStamp, CreateUserStamp

Schema (ngam.ngextractiondata):
    AnalysisID, RunID, iPosition, dTimeStart, dTimeEnd,
    fWeightGasBulbBefore, fWeightGasBulbAfter,
    Status, IsIgnored, nvcRemarks
"""
from __future__ import annotations
import sys
import logging
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QComboBox, QLineEdit,
    QPushButton, QTableView, QCheckBox, QLabel,
    QMessageBox, QHeaderView, QStyledItemDelegate, QDialog,
    QApplication,
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QBrush, QColor, QPainter
from PyQt5.QtCore import Qt, QRect, QCoreApplication

from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from help_browser import make_help_button
from shared_utils import check_employee_privilege, get_current_user_id, normalize_login_name

try:
    from ngam_ingrowth_create_run_gui import NGAMCreateRunDialog as NGAMExtractionCreateRunDialog
except ImportError:
    NGAMExtractionCreateRunDialog = None

try:
    from ngam_extraction_run_gui import NGAMExtractionRunWindow
except ImportError:
    NGAMExtractionRunWindow = None


# ─────────────────────────────── status delegate ─────────────────────────────
class _StatusDelegate(QStyledItemDelegate):
    """Coloured circle for run status column."""
    _COLOURS = {
        "Complete": QColor(50, 200, 50),
        "Ongoing":  QColor(255, 165, 0),
    }

    def paint(self, painter, option, index):
        if index.column() == 0:
            status = index.data(Qt.UserRole + 1)
            colour = self._COLOURS.get(status, QColor(200, 200, 200))
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing)
            sz = 12
            r = option.rect
            x = r.x() + (r.width() - sz) // 2
            y = r.y() + (r.height() - sz) // 2
            painter.setBrush(QBrush(colour))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QRect(x, y, sz, sz))
            painter.restore()
        else:
            super().paint(painter, option, index)


# ─────────────────────────────── main window ─────────────────────────────────
class NGAMExtractionRunsWindow(QWidget):
    """
    List, filter and open Noble Gas Extraction Runs.
    Used as a tab/panel inside the main launcher.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.main_layout = QVBoxLayout(self)

        # ── top bar ──────────────────────────────────────────────────────────
        top_bar = QHBoxLayout()
        top_bar.addStretch()

        self.btnNew = QPushButton("Create New Run")
        self.btnNew.setStyleSheet("""
            QPushButton { background-color: #27ae60; color: white; font-weight: bold;
                          border: none; padding: 5px 14px; border-radius: 4px; }
            QPushButton:hover   { background-color: #219a52; }
            QPushButton:pressed { background-color: #1e8449; }
            QPushButton:disabled{ background-color: #a9dfbf; color: #7f8c8d; }
        """)
        self.btnNew.clicked.connect(self.create_new_run)
        top_bar.addWidget(self.btnNew)

        self.btnClose = QPushButton("Close Module")
        self.btnClose.clicked.connect(self._close_module)
        top_bar.addWidget(self.btnClose)
        top_bar.addWidget(make_help_button(self, "ngam_extraction"))
        self.main_layout.addLayout(top_bar)
        self.main_layout.addSpacing(8)

        # ── filter group ──────────────────────────────────────────────────────
        self.main_layout.addWidget(self._create_filter_group())
        self.main_layout.addSpacing(12)

        # ── run table ─────────────────────────────────────────────────────────
        self.run_table = QTableView()
        self._apply_table_styling()
        self.status_delegate = _StatusDelegate()
        self.run_table.setItemDelegateForColumn(0, self.status_delegate)
        self.run_table.setSelectionBehavior(QTableView.SelectRows)
        self.run_table.setEditTriggers(QTableView.NoEditTriggers)
        self.run_table.setSortingEnabled(True)

        self.model = QStandardItemModel()
        self.run_table.setModel(self.model)
        self.run_table.clicked.connect(self._on_row_clicked)
        self.run_table.doubleClicked.connect(self._on_row_double_clicked)
        self.main_layout.addWidget(self.run_table, 1)

        # ── privileges ────────────────────────────────────────────────────────
        self.has_write_priv = False
        self.has_admin_priv = False
        self._check_privileges()
        self._update_ui_state()

        # ── initial load ──────────────────────────────────────────────────────
        try:
            db_manager.get_engine()
            self.load_run_list()
        except Exception as e:
            self.setEnabled(False)
            self.main_layout.addWidget(QLabel(f"DB Error: {e}"))

    # ── privileges ────────────────────────────────────────────────────────────

    def _check_privileges(self):
        try:
            user = normalize_login_name(get_current_user_id())
            self.has_write_priv = check_employee_privilege(user, "ngamaccess")
            self.has_admin_priv = check_employee_privilege(user, "ngamadmin")
        except Exception as e:
            logging.error(f"NGAM extraction privilege check failed: {e}")
            self.has_write_priv = False
            self.has_admin_priv = False

    def _update_ui_state(self):
        self.btnNew.setEnabled(self.has_write_priv)
        self.btnDelete.setEnabled(self.has_admin_priv)

    # ── table styling ─────────────────────────────────────────────────────────

    def _apply_table_styling(self):
        self.run_table.setShowGrid(False)
        self.run_table.setStyleSheet("""
            QTableView { border: none; background-color: white; }
            QTableView::item { padding: 6px 8px; border: none;
                                background-color: white; color: #333333; }
            QTableView::item:alternate { background-color: #F3F7FA; }
            QTableView::item:selected  { background-color: #DDEEFF; color: #000000; }
            QHeaderView::section { background-color: white; color: #7F8BB5;
                                   font-weight: bold; padding: 6px 8px;
                                   border: none; border-bottom: 2px solid #7F8BB5; }
            QHeaderView::section:hover { background-color: #DDEEFF; color: #000000; }
        """)
        self.run_table.setAlternatingRowColors(True)
        self.run_table.verticalHeader().setVisible(False)
        self.run_table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)

    # ── filter group ──────────────────────────────────────────────────────────

    def _create_filter_group(self):
        grp = QGroupBox("Filter / Search Noble Gas Extraction Runs")
        layout = QVBoxLayout()

        r1 = QHBoxLayout()
        self.chkShowOngoing = QCheckBox("Show Ongoing Runs Only")
        self.chkShowOngoing.setChecked(True)
        self.chkShowOngoing.toggled.connect(self.load_run_list)
        r1.addWidget(self.chkShowOngoing)
        r1.addStretch()
        layout.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Search By:"))

        self.cmbSearchType = QComboBox()
        self.cmbSearchType.addItems(["Extraction Run", "Sample ID", "Sample Name"])
        self.cmbSearchType.currentTextChanged.connect(self._on_search_type_changed)
        r2.addWidget(self.cmbSearchType)

        self.txtSearch = QLineEdit()
        self.txtSearch.setPlaceholderText("Enter search term…")
        self.txtSearch.returnPressed.connect(self._on_search_or_open)
        r2.addWidget(self.txtSearch, 1)

        self.btnSearch = QPushButton("Open Run")
        self.btnSearch.clicked.connect(self._on_search_or_open)
        r2.addWidget(self.btnSearch)

        self.btnReset = QPushButton("Reset")
        self.btnReset.clicked.connect(self._reset_filters)
        r2.addWidget(self.btnReset)

        r2.addSpacing(16)

        self.btnDelete = QPushButton("Delete")
        self.btnDelete.setStyleSheet("""
            QPushButton { background-color: #c0392b; color: white; font-weight: bold;
                          border: none; padding: 5px 14px; border-radius: 4px; }
            QPushButton:hover   { background-color: #a93226; }
            QPushButton:pressed { background-color: #922b21; }
            QPushButton:disabled{ background-color: #f1948a; color: #7f8c8d; }
        """)
        self.btnDelete.clicked.connect(self.delete_run)
        r2.addWidget(self.btnDelete)

        layout.addLayout(r2)
        grp.setLayout(layout)
        return grp

    # ── smart search button ───────────────────────────────────────────────────

    def _on_search_type_changed(self, text: str):
        self.btnSearch.setText("Open Run" if text == "Extraction Run" else "Search")

    def _on_search_or_open(self):
        if self.cmbSearchType.currentText() == "Extraction Run":
            val = self.txtSearch.text().strip()
            if not val:
                self.load_run_list()
                return
            try:
                run_id = int(val)
            except ValueError:
                show_message(self, "Invalid ID", "Extraction Run ID must be a number.", QMessageBox.Warning)
                return
            self.open_run_details(run_id)
        else:
            self.load_run_list()

    def _reset_filters(self):
        self.chkShowOngoing.setChecked(True)
        self.txtSearch.clear()
        self.cmbSearchType.setCurrentIndex(0)
        self.load_run_list()

    # ── query builder ─────────────────────────────────────────────────────────

    def _build_query(self):
        sql = """
            SELECT
                r.runid,
                r.runstarttime,
                r.runendtime,
                r.technicianid,
                r.runstatus,
                r.remarks,
                e.lastname || ', ' || e.firstmiddlename AS techname,
                eq.equipmentname,
                ap.procedurename
            FROM ngam.ngextractionrun r
            LEFT JOIN public.employee          e  ON e.employeeid   = r.technicianid
            LEFT JOIN public.equipment         eq ON eq.equipmentid = r.equipmentid
            LEFT JOIN public.analysisprocedure ap ON ap.procedureid = r.procedureid
        """

        wheres = []
        params: dict = {}

        if self.chkShowOngoing.isChecked():
            wheres.append("r.runendtime IS NULL")

        s_val  = self.txtSearch.text().strip()
        s_type = self.cmbSearchType.currentText()

        if s_val:
            if s_type == "Extraction Run" and s_val.isdigit():
                wheres.append("r.runid = :rid")
                params["rid"] = int(s_val)
            elif s_type == "Sample ID" and s_val.isdigit():
                wheres.append("""
                    r.runid IN (
                        SELECT DISTINCT d.runid FROM ngam.ngextractiondata d
                        JOIN public.analysis a ON d.analysisid = a.analysisid
                        WHERE a.sampleid = :sid
                    )
                """)
                params["sid"] = int(s_val)
            elif s_type == "Sample Name":
                wheres.append("""
                    r.runid IN (
                        SELECT DISTINCT d.runid FROM ngam.ngextractiondata d
                        JOIN public.analysis a ON d.analysisid = a.analysisid
                        JOIN public.sample   s ON a.sampleid = s.sampleid
                                               AND a.prefix = s.prefix
                        WHERE s.sname ILIKE :sname
                    )
                """)
                params["sname"] = f"%{s_val}%"

        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY r.runstarttime DESC, r.runid DESC"
        return sql, params

    # ── data loading ──────────────────────────────────────────────────────────

    def load_run_list(self):
        self.model.clear()
        self.model.setHorizontalHeaderLabels(
            ["Status", "Run ID", "Start Date", "End Date",
             "Technician", "Equipment", "Procedure", "Remarks"]
        )
        sql, params = self._build_query()
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(sql), params).fetchall()

            for r in rows:
                status = "Complete" if r[2] else "Ongoing"
                st_item = QStandardItem(status)
                st_item.setData(status, Qt.UserRole + 1)

                id_item = QStandardItem(str(r[0]))
                id_item.setData(r[0], Qt.UserRole)

                start_s = r[1].strftime("%Y-%m-%d %H:%M") if r[1] else ""
                end_s   = r[2].strftime("%Y-%m-%d %H:%M") if r[2] else ""

                self.model.appendRow([
                    st_item, id_item,
                    QStandardItem(start_s), QStandardItem(end_s),
                    QStandardItem(r[6] or str(r[3] or "")),
                    QStandardItem(r[7] or ""),
                    QStandardItem(r[8] or ""),
                    QStandardItem(r[5] or ""),
                ])

            h = self.run_table.horizontalHeader()
            for i in range(7):
                h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
            h.setSectionResizeMode(7, QHeaderView.Stretch)

        except Exception as e:
            logging.error(f"NGAM extraction load_run_list failed: {e}")
            show_message(self, "Error", f"Failed to load runs:\n{e}", QMessageBox.Warning)

    # ── row interaction ───────────────────────────────────────────────────────

    def _on_row_clicked(self, index):
        if not index.isValid():
            return
        item = self.model.item(index.row(), 1)
        if item and item.data(Qt.UserRole) is not None:
            self.cmbSearchType.setCurrentText("Extraction Run")
            self.txtSearch.setText(str(item.data(Qt.UserRole)))

    def _on_row_double_clicked(self, index):
        if not index.isValid():
            return
        item = self.model.item(index.row(), 1)
        if item:
            self.open_run_details(item.data(Qt.UserRole))

    def _get_selected_run_id(self):
        idx = self.run_table.selectionModel().currentIndex()
        if not idx.isValid():
            show_message(self, "No Selection", "Please select a run first.", QMessageBox.Warning)
            return None
        item = self.model.item(idx.row(), 1)
        return item.data(Qt.UserRole) if item else None

    # ── actions ───────────────────────────────────────────────────────────────

    def create_new_run(self):
        if not NGAMExtractionCreateRunDialog:
            show_message(self, "Error", "Create Run module not loaded.", QMessageBox.Critical)
            return
        dlg = NGAMExtractionCreateRunDialog(self, mode="extraction")
        if dlg.exec_() == QDialog.Accepted:
            self.load_run_list()

    def open_run_details(self, run_id):
        if not NGAMExtractionRunWindow:
            show_message(self, "Error", "Run Details module not loaded.", QMessageBox.Critical)
            return
        dlg = NGAMExtractionRunWindow(run_id=int(run_id), parent=self)
        dlg.finished.connect(self._on_details_closed)
        dlg.show()

    def _on_details_closed(self, _result):
        self._reset_filters()

    def delete_run(self):
        rid = self._get_selected_run_id()
        if not rid:
            return
        reply = QMessageBox.question(
            self, "Delete Run",
            f"Delete Extraction Run {rid} and all its sample data?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            with db_manager.get_connection() as conn:
                # Select associated analysis IDs first
                sample_rows = conn.execute(text("""
                    SELECT DISTINCT analysisid FROM ngam.ngextractiondata
                    WHERE runid = :rid AND analysisid IS NOT NULL
                """), {"rid": rid}).fetchall()
                analysis_ids = [r[0] for r in sample_rows]

                if analysis_ids:
                    # Guard: refuse if any extraction analysisid is already in a sequence run.
                    in_sequence = conn.execute(text("""
                        SELECT DISTINCT r.runid FROM ngam.ngpreparations p
                        JOIN ngam.ngsequencerun r ON r.runid = p.runid
                        WHERE p.analysisid = ANY(:aids)
                        LIMIT 1
                    """), {"aids": analysis_ids}).fetchone()
                    if in_sequence:
                        show_message(self, "Delete Blocked",
                            f"Cannot delete: extraction results are used by sequence run #{in_sequence[0]}.\n"
                            f"Delete that sequence run first.", QMessageBox.Warning)
                        return

                    # Delete children + analysis cascade
                    conn.execute(text("DELETE FROM ngam.ngextractiondata WHERE runid = :rid"), {"rid": rid})
                    conn.execute(text("DELETE FROM public.finalvalue WHERE analysisid = ANY(:aids)"), {"aids": analysis_ids})
                    conn.execute(text("DELETE FROM public.analysis_repeat WHERE analysisid = ANY(:aids)"), {"aids": analysis_ids})
                    conn.execute(text("DELETE FROM public.analysis WHERE analysisid = ANY(:aids)"), {"aids": analysis_ids})
                else:
                    conn.execute(text("DELETE FROM ngam.ngextractiondata WHERE runid = :rid"), {"rid": rid})

                conn.execute(
                    text("DELETE FROM ngam.ngextractionrun WHERE runid = :rid"),
                    {"rid": rid}
                )
                conn.commit()
            self.load_run_list()
        except Exception as e:
            logging.error(f"NGAM extraction delete_run failed: {e}")
            show_message(self, "Delete Error", str(e), QMessageBox.Critical)

    # ── launcher interface ────────────────────────────────────────────────────

    def refresh(self):
        self.load_run_list()

    def _close_module(self):
        if isinstance(self.parent(), QWidget):
            self.close()
        else:
            QCoreApplication.instance().quit()


def load_ngam_extraction_runs():
    return NGAMExtractionRunsWindow()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    w = NGAMExtractionRunsWindow()
    w.show()
    sys.exit(app.exec_())
