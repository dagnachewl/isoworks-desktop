"""
ngam_sequence_runs_gui.py
=========================
NGAM – Noble Gas Analysis Module
3He Sequence / Mass-Spectrometer Measurement Run List

Mirrors ngam_ingrowth_runs_gui.py pattern exactly.

Privilege roles (Employee table columns):
    ngamaccess  → create runs, edit data
    ngamadmin   → delete runs
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
    from ngam_ingrowth_create_run_gui import NGAMIngrowthCreateRunDialog
except ImportError:
    NGAMIngrowthCreateRunDialog = None

try:
    from ngam_sequence_run_gui import NGAMSequenceRunWindow
except ImportError:
    NGAMSequenceRunWindow = None


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
class NGAMSequenceRunsWindow(QWidget):
    """
    List, filter and open 3He Sequence / Measurement Runs.
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
        top_bar.addWidget(make_help_button(self, "ngam_sequence"))
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
            logging.info(
                f"NGAM sequence user {user}: write={self.has_write_priv}, admin={self.has_admin_priv}"
            )
        except Exception as e:
            logging.error(f"NGAM sequence privilege check failed: {e}", exc_info=True)
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
        grp = QGroupBox("Filter / Search 3He Sequence Measurement Runs")
        layout = QVBoxLayout()

        # row 1: checkbox filters
        r1 = QHBoxLayout()
        self.chkShowOngoing = QCheckBox("Show Ongoing Runs Only")
        self.chkShowOngoing.setChecked(True)
        self.chkShowOngoing.toggled.connect(self.load_run_list)
        r1.addWidget(self.chkShowOngoing)
        r1.addStretch()
        layout.addLayout(r1)

        # row 2: search + actions
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Search By:"))

        self.cmbSearchType = QComboBox()
        self.cmbSearchType.addItems(["Sequence Run", "Sample ID", "Sample Name", "Project Name"])
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
        self.btnSearch.setText("Open Run" if text == "Sequence Run" else "Search")

    def _on_search_or_open(self):
        if self.cmbSearchType.currentText() == "Sequence Run":
            val = self.txtSearch.text().strip()
            if not val:
                self.load_run_list()
                return
            try:
                run_id = int(val)
            except ValueError:
                show_message(self, "Invalid ID", "Sequence Run ID must be a number.", QMessageBox.Warning)
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
        dialect = getattr(db_manager, "dialect", "SQL_SERVER")

        def cast(expr):
            return (f"CAST({expr} AS NVARCHAR(255))" if dialect == "SQL_SERVER"
                    else f"CAST({expr} AS TEXT)")

        sep = " + " if dialect == "SQL_SERVER" else " || "
        tech_name = sep.join([cast("e.LastName"), "', '", cast("e.FirstMiddleName")])

        sql = f"""
            SELECT
                r.RunID,
                r.RunStartTime,
                r.RunEndTime,
                r.TechnicianID,
                r.RunStatus,
                r.Remarks,
                {tech_name} AS TechName,
                eq.EquipmentName
            FROM ngam.msrun r
            LEFT JOIN Employee   e  ON r.TechnicianID = e.EmployeeID
            LEFT JOIN Equipment  eq ON r.EquipmentID  = eq.EquipmentID
        """

        wheres = ["r.measurement_mode = 'IG'"]
        params: dict = {}

        if self.chkShowOngoing.isChecked():
            wheres.append("r.RunEndTime IS NULL")

        s_val  = self.txtSearch.text().strip()
        s_type = self.cmbSearchType.currentText()

        if s_val:
            if s_type == "Sequence Run" and s_val.isdigit():
                wheres.append("r.RunID = :rid")
                params["rid"] = int(s_val)
            elif s_type == "Sample ID" and s_val.isdigit():
                wheres.append("""
                    r.RunID IN (
                        SELECT DISTINCT sl.RunID FROM NGAM.NG3HeSequenceLoadList sl
                        JOIN Analysis a ON sl.AnalysisID = a.AnalysisID
                        WHERE a.SampleID = :sid
                    )
                """)
                params["sid"] = int(s_val)
            elif s_type == "Sample Name":
                wheres.append("""
                    r.RunID IN (
                        SELECT DISTINCT sl.RunID FROM NGAM.NG3HeSequenceLoadList sl
                        JOIN Analysis a ON sl.AnalysisID = a.AnalysisID
                        JOIN Sample   s ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix
                        WHERE s.sName LIKE :sname
                    )
                """)
                params["sname"] = f"%{s_val}%"
            elif s_type == "Project Name":
                wheres.append("""
                    r.RunID IN (
                        SELECT DISTINCT sl.RunID FROM NGAM.NG3HeSequenceLoadList sl
                        JOIN Analysis a ON sl.AnalysisID = a.AnalysisID
                        JOIN Sample   s ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix
                        JOIN Submission sub ON s.SubmissionID = sub.SubmissionID
                        WHERE sub.SubmissionName LIKE :pname
                    )
                """)
                params["pname"] = f"%{s_val}%"

        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY r.RunStartTime DESC, r.RunID DESC"
        return sql, params

    # ── data loading ──────────────────────────────────────────────────────────

    def load_run_list(self):
        self.model.clear()
        self.model.setHorizontalHeaderLabels(
            ["Status", "Run ID", "Start Date", "End Date",
             "Technician", "Equipment", "Remarks"]
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

                start_s = r[1].strftime("%Y-%m-%d") if r[1] else ""
                end_s   = r[2].strftime("%Y-%m-%d") if r[2] else ""

                self.model.appendRow([
                    st_item, id_item,
                    QStandardItem(start_s), QStandardItem(end_s),
                    QStandardItem(r[6] or str(r[3] or "")),
                    QStandardItem(r[7] or ""),
                    QStandardItem(r[5] or ""),
                ])

            h = self.run_table.horizontalHeader()
            for i in range(6):
                h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
            h.setSectionResizeMode(6, QHeaderView.Stretch)

        except Exception as e:
            logging.error(f"NGAM sequence load_run_list failed: {e}")
            show_message(self, "Error", f"Failed to load runs:\n{e}", QMessageBox.Warning)

    # ── row interaction ───────────────────────────────────────────────────────

    def _on_row_clicked(self, index):
        if not index.isValid():
            return
        item = self.model.item(index.row(), 1)
        if item and item.data(Qt.UserRole) is not None:
            self.cmbSearchType.setCurrentText("Sequence Run")
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
        if not NGAMIngrowthCreateRunDialog:
            show_message(self, "Error", "Create Run module not loaded.", QMessageBox.Critical)
            return
        dlg = NGAMIngrowthCreateRunDialog(self, mode="sequence")
        if dlg.exec_() == QDialog.Accepted:
            self.load_run_list()

    def open_run_details(self, run_id):
        if not NGAMSequenceRunWindow:
            show_message(self, "Error", "Sequence Run Details module not loaded.", QMessageBox.Critical)
            return
        dlg = NGAMSequenceRunWindow(run_id=int(run_id), parent=self)
        dlg.finished.connect(self._on_details_closed)
        dlg.show()

    def _on_details_closed(self, _result):
        self._reset_filters()

    def delete_run(self):
        rid = self._get_selected_run_id()
        if not rid:
            return
        reply = QMessageBox.question(
            self, "Delete Sequence Run",
            f"Delete Sequence Run {rid} and all its load list data?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            with db_manager.get_connection() as conn:
                # Collect non-sample (blank/repro/lin) analysis records minted at run creation
                non_sample_aids = [r[0] for r in conn.execute(text("""
                    SELECT ll.analysisid FROM ngam.ng3hesequenceloadlist ll
                    WHERE ll.runid = :rid
                      AND (ll.bisblank = TRUE OR ll.bisreproreference = TRUE OR ll.bislinreference = TRUE)
                      AND ll.analysisid IS NOT NULL
                """), {"rid": rid}).fetchall()]

                # Delete raw data and load list
                conn.execute(
                    text("DELETE FROM NGAM.NG3HeSequenceLoadList WHERE RunID = :rid"),
                    {"rid": rid}
                )
                conn.execute(
                    text("DELETE FROM ngam.msrun WHERE runid = :rid AND measurement_mode = 'IG'"),
                    {"rid": rid}
                )

                # Delete non-sample analysis records (cleared now that loadlist FK references are gone)
                if non_sample_aids:
                    conn.execute(text("DELETE FROM public.finalvalue WHERE analysisid = ANY(:aids)"), {"aids": non_sample_aids})
                    conn.execute(text("DELETE FROM public.analysis_repeat WHERE analysisid = ANY(:aids)"), {"aids": non_sample_aids})
                    conn.execute(text("DELETE FROM public.analysis WHERE analysisid = ANY(:aids)"), {"aids": non_sample_aids})

                conn.commit()
            self.load_run_list()
        except Exception as e:
            logging.error(f"NGAM sequence delete_run failed: {e}")
            show_message(self, "Delete Error", str(e), QMessageBox.Critical)

    # ── launcher interface ────────────────────────────────────────────────────

    def refresh(self):
        self.load_run_list()

    def _close_module(self):
        if isinstance(self.parent(), QWidget):
            self.close()
        else:
            QCoreApplication.instance().quit()


def load_ngam_sequence_runs():
    return NGAMSequenceRunsWindow()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    w = NGAMSequenceRunsWindow()
    w.show()
    sys.exit(app.exec_())
