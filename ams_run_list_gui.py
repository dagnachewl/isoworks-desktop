"""
ams_run_list_gui.py — AMS 14C run list panel for IsoWorks.
Provides AMSRunListWindow (QWidget) for listing, filtering, creating,
and opening AMS analysis runs.
"""
from __future__ import annotations
import logging

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QComboBox, QPushButton, QTableView, QLabel,
    QHeaderView, QMessageBox, QDialog, QStyledItemDelegate
)
from PyQt5.QtGui import (
    QStandardItemModel, QStandardItem, QBrush, QColor, QPainter
)
from PyQt5.QtCore import Qt, QRect, QSettings, QCoreApplication

from db_core import db_manager
from sqlalchemy import text
from shared_utils import (
    check_employee_privilege, get_current_user_id, normalize_login_name
)
from gui_utils import EmbeddedSearchBox
from help_browser import make_help_button

try:
    from ams_create_run_gui import AMSCreateRunDialog
except ImportError:
    AMSCreateRunDialog = None

try:
    from ams_run_details_gui import AMSRunDetailsWindow
except ImportError:
    AMSRunDetailsWindow = None

log = logging.getLogger(__name__)

# ── Status colours ─────────────────────────────────────────────────────────────
_STATUS_LABELS = {0: "Open", 1: "Reduced", 2: "Approved", 3: "Locked"}
_STATUS_COLORS = {
    0: QColor(255, 165,  0),   # orange  — open
    1: QColor( 30, 144, 255),  # blue    — reduced
    2: QColor( 50, 200,  50),  # green   — approved
    3: QColor(150, 150, 150),  # grey    — locked
}

_TABLE_STYLE = """
QTableView { border: none; background: white; gridline-color: transparent; }
QTableView::item { padding: 6px 8px; border: none; color: #333; }
QTableView::item:alternate { background: #F3F7FA; }
QTableView::item:selected { background: #DDEEFF; color: #000; }
QHeaderView::section { background: white; color: #7F8BB5; font-weight: bold;
    padding: 6px 8px; border: none; border-bottom: 2px solid #7F8BB5; }
QHeaderView::section:hover { background: #DDEEFF; color: #000; }
"""


class _StatusDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        status_val = index.data(Qt.UserRole + 1)
        color = _STATUS_COLORS.get(status_val)
        if color is None:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        sz = 12
        x = option.rect.x() + (option.rect.width()  - sz) // 2
        y = option.rect.y() + (option.rect.height() - sz) // 2
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QRect(x, y, sz, sz))
        painter.restore()


class AMSRunListWindow(QWidget):
    """List, filter, create and open AMS runs."""

    _HEADERS = [
        "Status", "Run ID", "Run Code", "Date",
        "Equipment", "Technician", "Wheels", "Targets", "Remarks"
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = QSettings("YourOrg", "ISOTOPES_SUITE")
        self._details_window = None
        self.has_write_priv  = False
        self.has_admin_priv  = False

        layout = QVBoxLayout(self)
        layout.addLayout(self._build_action_bar())
        layout.addSpacing(10)
        layout.addWidget(self._build_filter_group())
        layout.addSpacing(10)

        self._model = QStandardItemModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._table.setSelectionBehavior(QTableView.SelectRows)
        self._table.setEditTriggers(QTableView.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.setStyleSheet(_TABLE_STYLE)
        self._table.setItemDelegateForColumn(0, _StatusDelegate())
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.clicked.connect(self._on_click)
        layout.addWidget(self._table, 1)

        self._check_privileges()
        self._update_ui_state()

        try:
            db_manager.get_engine()
            self._load_filter_combos()
            self.load_run_list()
        except Exception:
            self.setEnabled(False)
            lbl = QLabel("<h2>No Database Connection</h2>"
                         "<p>Please configure in Settings.</p>")
            lbl.setAlignment(Qt.AlignCenter)
            layout.addWidget(lbl)

    # ── Privileges ─────────────────────────────────────────────────────────────

    def _check_privileges(self):
        try:
            u = normalize_login_name(get_current_user_id())
            self.has_write_priv = check_employee_privilege(u, "accessams")
            self.has_admin_priv = check_employee_privilege(u, "amsadmin")
        except Exception as exc:
            log.error("AMS privilege check: %s", exc)

    def _update_ui_state(self):
        self.btnCreate.setEnabled(self.has_write_priv)
        self.btnDelete.setEnabled(self.has_admin_priv)

    # ── UI builders ────────────────────────────────────────────────────────────

    def _build_action_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.addStretch()

        self.btnCreate = QPushButton("Create New Run")
        self.btnCreate.setStyleSheet("""
            QPushButton { background:#27ae60; color:white; font-weight:bold;
                          border:none; padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background:#219a52; }
            QPushButton:disabled { background:#a9dfbf; color:#7f8c8d; }
        """)
        self.btnDelete = QPushButton("Delete Run")
        self.btnDelete.setStyleSheet("""
            QPushButton { background:#c0392b; color:white; font-weight:bold;
                          border:none; padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background:#a93226; }
            QPushButton:disabled { background:#f1948a; color:#7f8c8d; }
        """)
        self.btnBATSImport = QPushButton("Import BATS Files")
        self.btnBATSImport.setStyleSheet("""
            QPushButton { background:#1976d2; color:white; font-weight:bold;
                          border:none; padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background:#1565c0; }
            QPushButton:disabled { background:#90caf9; }
        """)
        btnClose = QPushButton("Close")

        self.btnCreate.clicked.connect(self._create_run)
        self.btnDelete.clicked.connect(self._delete_run)
        self.btnBATSImport.clicked.connect(self._open_bats_import)
        btnClose.clicked.connect(self._close_module)

        for w in [self.btnCreate, self.btnDelete, self.btnBATSImport, btnClose,
                  make_help_button(self, "ams_run_list")]:
            bar.addWidget(w)
        return bar

    def _build_filter_group(self) -> QGroupBox:
        grp = QGroupBox("Filter / Search AMS Runs")
        vlay = QVBoxLayout(grp)

        top = QGridLayout()
        top.addWidget(QLabel("Status:"), 0, 0)
        self.cmbStatus = QComboBox()
        self.cmbStatus.addItem("— All —", None)
        for code, label in _STATUS_LABELS.items():
            self.cmbStatus.addItem(label, code)
        top.addWidget(self.cmbStatus, 0, 1)

        top.addWidget(QLabel("Equipment:"), 0, 2)
        self.cmbEquipFilter = QComboBox()
        top.addWidget(self.cmbEquipFilter, 0, 3)
        top.setColumnStretch(1, 1)
        top.setColumnStretch(3, 1)
        vlay.addLayout(top)

        bot = QHBoxLayout()
        bot.addWidget(QLabel("Search By:"))
        self.cmbSearchType = QComboBox()
        self.cmbSearchType.addItems(["Run ID", "Run Code", "Sample ID"])
        self.cmbSearchType.currentTextChanged.connect(self._on_search_type_changed)
        bot.addWidget(self.cmbSearchType)

        self.txtSearch = EmbeddedSearchBox(action_text="Open Run →")
        self.txtSearch.setPlaceholderText("Enter search term…")
        self.txtSearch.action_clicked.connect(self._on_search_or_open)
        self.txtSearch.clear_clicked.connect(self._reset_filters)
        bot.addWidget(self.txtSearch, 1)
        vlay.addLayout(bot)

        self.cmbStatus.currentIndexChanged.connect(self.load_run_list)
        self.cmbEquipFilter.currentIndexChanged.connect(self.load_run_list)
        return grp

    # ── Data loading ───────────────────────────────────────────────────────────

    def _load_filter_combos(self):
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT DISTINCT e.EquipmentID, e.EquipmentName
                    FROM Equipment e
                    INNER JOIN ams.amsrun r ON r.equipmentid = e.EquipmentID
                    ORDER BY e.EquipmentName
                """)).fetchall()
            self.cmbEquipFilter.addItem("— All Equipment —", None)
            for r in rows:
                self.cmbEquipFilter.addItem(r.EquipmentName, r.EquipmentID)
        except Exception as exc:
            log.warning("AMS filter combos: %s", exc)

    def load_run_list(self):
        params: dict = {}
        clauses: list[str] = []

        status_val = self.cmbStatus.currentData()
        if status_val is not None:
            clauses.append("r.runstatus = :status")
            params["status"] = status_val

        equip_id = self.cmbEquipFilter.currentData()
        if equip_id is not None:
            clauses.append("r.equipmentid = :eid")
            params["eid"] = equip_id

        stype = self.cmbSearchType.currentText()
        sval  = self.txtSearch.text().strip()
        if sval:
            try:
                if stype == "Run ID":
                    clauses.append("r.amsrunid = :rid")
                    params["rid"] = int(sval)
                elif stype == "Run Code":
                    clauses.append("r.runcode ILIKE :code")
                    params["code"] = f"%{sval}%"
                elif stype == "Sample ID":
                    clauses.append("""
                        r.amsrunid IN (
                            SELECT w.amsrunid FROM ams.amswheel w
                            JOIN ams.amstarget t ON t.amswheelid = w.amswheelid
                            JOIN analysis a ON a.analysisid = t.analysisid
                            WHERE a.sampleid = :sid
                        )
                    """)
                    params["sid"] = int(sval)
            except ValueError:
                QMessageBox.warning(self, "Search", "Invalid search value.")
                return

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        sql = f"""
            SELECT r.amsrunid, r.runcode, r.rundate,
                   e.EquipmentName,
                   {db_manager.sql_concat('emp.LastName', "', '", 'emp.FirstMiddleName')} AS technician,
                   r.runstatus, r.notes,
                   COUNT(DISTINCT w.amswheelid)  AS nwheels,
                   COUNT(DISTINCT t.amstargetid) AS ntargets
            FROM ams.amsrun r
            LEFT JOIN Equipment  e   ON e.EquipmentID  = r.equipmentid
            LEFT JOIN Employee   emp ON emp.EmployeeID = r.technicianid
            LEFT JOIN ams.amswheel  w ON w.amsrunid    = r.amsrunid
            LEFT JOIN ams.amstarget t ON t.amswheelid  = w.amswheelid
            {where}
            GROUP BY r.amsrunid, r.runcode, r.rundate,
                     e.EquipmentName, emp.LastName, emp.FirstMiddleName,
                     r.runstatus, r.notes
            ORDER BY r.amsrunid DESC
        """

        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(sql), params).fetchall()
        except Exception as exc:
            log.error("AMS load_run_list: %s", exc)
            return

        self._model.clear()
        self._model.setHorizontalHeaderLabels(self._HEADERS)

        for row in rows:
            status_code = row.runstatus or 0
            status_item = QStandardItem(_STATUS_LABELS.get(status_code, "?"))
            status_item.setData(status_code, Qt.UserRole + 1)

            id_item = QStandardItem(str(row.amsrunid))
            id_item.setData(row.amsrunid, Qt.UserRole)

            self._model.appendRow([
                status_item,
                id_item,
                QStandardItem(row.runcode or ""),
                QStandardItem(str(row.rundate) if row.rundate else ""),
                QStandardItem(row.EquipmentName or ""),
                QStandardItem(row.technician or ""),
                QStandardItem(str(row.nwheels)),
                QStandardItem(str(row.ntargets)),
                QStandardItem(row.notes or ""),
            ])

        h = self._table.horizontalHeader()
        for i in range(4):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        for i in range(4, len(self._HEADERS)):
            h.setSectionResizeMode(i, QHeaderView.Stretch)

    def refresh(self):
        self.load_run_list()

    # ── Interaction ────────────────────────────────────────────────────────────

    def _get_selected_run_id(self) -> int | None:
        idx = self._table.selectionModel().currentIndex()
        if not idx.isValid():
            QMessageBox.warning(self, "No Selection", "Please select a run first.")
            return None
        item = self._model.item(idx.row(), 1)
        return item.data(Qt.UserRole) if item else None

    def _on_click(self, index):
        item = self._model.item(index.row(), 1)
        if item:
            rid = item.data(Qt.UserRole)
            if rid is not None:
                self.cmbSearchType.setCurrentText("Run ID")
                self.txtSearch.setText(str(rid))

    def _on_double_click(self, _index):
        rid = self._get_selected_run_id()
        if rid:
            self._open_run_details(rid)

    def _on_search_type_changed(self, text: str):
        self.txtSearch.set_action_text(
            "Open Run →" if text == "Run ID" else "Search →"
        )

    def _on_search_or_open(self):
        if self.cmbSearchType.currentText() == "Run ID":
            val = self.txtSearch.text().strip()
            if not val:
                self.load_run_list()
                return
            try:
                self._open_run_details(int(val))
            except ValueError:
                QMessageBox.warning(self, "Invalid ID", "AMS Run ID must be a number.")
        else:
            self.load_run_list()

    def _reset_filters(self):
        self.txtSearch.clear()
        self.cmbSearchType.setCurrentIndex(0)
        self.cmbStatus.setCurrentIndex(0)
        self.cmbEquipFilter.setCurrentIndex(0)
        self.load_run_list()

    # ── Actions ────────────────────────────────────────────────────────────────

    def _create_run(self):
        if AMSCreateRunDialog is None:
            QMessageBox.warning(self, "Missing", "ams_create_run_gui not found.")
            return
        dlg = AMSCreateRunDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            self.load_run_list()
            # Auto-open details for the new run
            if dlg.new_run_id:
                self._open_run_details(dlg.new_run_id)

    def _open_run_details(self, run_id: int):
        if AMSRunDetailsWindow is None:
            QMessageBox.warning(self, "Missing", "ams_run_details_gui not found.")
            return
        if self._details_window:
            try:
                self._details_window.close()
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        win = AMSRunDetailsWindow(run_id=run_id, parent=self)
        self._details_window = win
        win.finished.connect(lambda _: self.load_run_list())
        win.show()

    def _delete_run(self):
        rid = self._get_selected_run_id()
        if not rid:
            return
        if QMessageBox.question(
            self, "Confirm Delete",
            f"Permanently delete AMS Run {rid} and all its wheels, targets,\n"
            "measurements and results?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ) == QMessageBox.No:
            return
        try:
            with db_manager.get_connection() as conn:
                # CASCADE on FK handles child rows automatically
                conn.execute(text(
                    "DELETE FROM ams.amsrun WHERE amsrunid = :rid"
                ), {"rid": rid})
                conn.commit()
            self.load_run_list()
        except Exception as exc:
            log.error("AMS delete run %d: %s", rid, exc)
            QMessageBox.critical(self, "Error", str(exc))

    def _open_bats_import(self):
        try:
            from ams_bats_import_gui import BATSImportDialog
        except ImportError as exc:
            QMessageBox.critical(self, "Import Error", str(exc))
            return
        dlg = BATSImportDialog(self)
        dlg.import_completed.connect(self.load_run_list)
        dlg.exec_()

    def _close_module(self):
        if isinstance(self.parent(), QWidget):
            self.close()
        else:
            QCoreApplication.instance().quit()
