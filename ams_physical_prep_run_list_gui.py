"""
ams_physical_prep_run_list_gui.py — Physical Prep batch list for IsoWorks AMS.
Provides PhysicalPrepRunListWindow (QWidget) for browsing, creating and
opening physical prep batches.
"""
from __future__ import annotations
import logging
from datetime import date, datetime

from PyQt5.QtWidgets import (
    QApplication, QWidget, QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QComboBox, QPushButton, QTableView, QLabel, QLineEdit,
    QTextEdit, QDateEdit, QFormLayout, QDialogButtonBox, QHeaderView,
    QMessageBox, QStyledItemDelegate,
)
from PyQt5.QtCore import Qt, QRect, QDate, QCoreApplication
from PyQt5.QtGui import (
    QStandardItemModel, QStandardItem, QBrush, QColor, QPainter,
)

from db_core import db_manager
from sqlalchemy import text
from shared_utils import (
    check_employee_privilege, get_current_user_id, normalize_login_name,
)
from gui_utils import EmbeddedSearchBox, show_message
from help_browser import make_help_button

try:
    from ams_physical_prep_run_details_gui import PhysicalPrepRunDetailsWindow
except ImportError:
    PhysicalPrepRunDetailsWindow = None

try:
    from ams_physical_prep_create_run_gui import PhysicalPrepCreateRunDialog
except ImportError:
    PhysicalPrepCreateRunDialog = None

log = logging.getLogger(__name__)

# ── Status palette ─────────────────────────────────────────────────────────────
_STATUS_LABELS = {0: "Open", 1: "Complete", 2: "Approved", 3: "Locked"}
_STATUS_COLORS = {
    0: QColor(255, 165,   0),   # orange  — open
    1: QColor( 30, 144, 255),   # blue    — complete
    2: QColor( 50, 200,  50),   # green   — approved
    3: QColor(150, 150, 150),   # grey    — locked
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


# ── Status dot delegate ────────────────────────────────────────────────────────

class _StatusDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        code  = index.data(Qt.UserRole + 1)
        color = _STATUS_COLORS.get(code)
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


# ── Main list window ───────────────────────────────────────────────────────────

class PhysicalPrepRunListWindow(QWidget):
    """Browse, create and open physical prep batches."""

    _HEADERS = [
        "Status", "Batch ID", "Batch Code", "Date",
        "Equipment", "Technician", "Samples", "Accepted", "Notes",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.has_write_priv = False
        self.has_admin_priv = False
        self._details_window = None

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
            log.error("Physical Prep privilege check: %s", exc)

    def _update_ui_state(self):
        self.btnCreate.setEnabled(self.has_write_priv)
        self.btnDelete.setEnabled(self.has_admin_priv)

    # ── UI builders ────────────────────────────────────────────────────────────

    def _build_action_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.addStretch()

        self.btnCreate = QPushButton("New Batch")
        self.btnCreate.setStyleSheet("""
            QPushButton { background:#27ae60; color:white; font-weight:bold;
                          border:none; padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background:#219a52; }
            QPushButton:disabled { background:#a9dfbf; color:#7f8c8d; }
        """)
        self.btnDelete = QPushButton("Delete Batch")
        self.btnDelete.setStyleSheet("""
            QPushButton { background:#c0392b; color:white; font-weight:bold;
                          border:none; padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background:#a93226; }
            QPushButton:disabled { background:#f1948a; color:#7f8c8d; }
        """)
        btnClose = QPushButton("Close")

        self.btnCreate.clicked.connect(self._create_batch)
        self.btnDelete.clicked.connect(self._delete_batch)
        btnClose.clicked.connect(self._close_module)

        for w in [self.btnCreate, self.btnDelete, btnClose,
                  make_help_button(self, "physicalprep_run_list")]:
            bar.addWidget(w)
        return bar

    def _build_filter_group(self) -> QGroupBox:
        grp = QGroupBox("Filter / Search Physical Prep Batches")
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
        self.cmbSearchType.addItems(["Batch ID", "Batch Code", "Analysis ID"])
        self.cmbSearchType.currentTextChanged.connect(self._on_search_type_changed)
        bot.addWidget(self.cmbSearchType)

        self.txtSearch = EmbeddedSearchBox(action_text="Open Batch →")
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
                    INNER JOIN ams.physicalprep_run r ON r.equipmentid = e.EquipmentID
                    ORDER BY e.EquipmentName
                """)).fetchall()
            self.cmbEquipFilter.addItem("— All Equipment —", None)
            for r in rows:
                self.cmbEquipFilter.addItem(r[1], r[0])
        except Exception as exc:
            log.warning("Physical Prep filter combos: %s", exc)
            self.cmbEquipFilter.addItem("— All Equipment —", None)

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
                if stype == "Batch ID":
                    clauses.append("r.physicalpreprunid = :bid")
                    params["bid"] = int(sval)
                elif stype == "Batch Code":
                    clauses.append("r.batchcode ILIKE :code")
                    params["code"] = f"%{sval}%"
                elif stype == "Analysis ID":
                    clauses.append("""
                        r.physicalpreprunid IN (
                            SELECT ps.physicalpreprunid FROM ams.physicalprep_sample ps
                            WHERE ps.analysisid = :aid
                        )
                    """)
                    params["aid"] = int(sval)
            except ValueError:
                QMessageBox.warning(self, "Search", "Invalid search value.")
                return

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        sql = f"""
            SELECT r.physicalpreprunid, r.batchcode, r.batchdate,
                   e.EquipmentName,
                   {db_manager.sql_concat('emp.LastName', "', '", 'emp.FirstMiddleName')} AS technician,
                   r.runstatus, r.notes,
                   COUNT(ps.physicalprepsampleid)                           AS nsamples,
                   COUNT(ps.physicalprepsampleid) FILTER (WHERE ps.isaccepted) AS naccepted
            FROM ams.physicalprep_run r
            LEFT JOIN Equipment  e   ON e.EquipmentID  = r.equipmentid
            LEFT JOIN Employee   emp ON emp.EmployeeID = r.technicianid
            LEFT JOIN ams.physicalprep_sample ps ON ps.physicalpreprunid = r.physicalpreprunid
            {where}
            GROUP BY r.physicalpreprunid, r.batchcode, r.batchdate,
                     e.EquipmentName, emp.LastName, emp.FirstMiddleName,
                     r.runstatus, r.notes
            ORDER BY r.physicalpreprunid DESC
        """

        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(sql), params).fetchall()
        except Exception as exc:
            log.error("Physical Prep load_run_list: %s", exc)
            return

        self._model.clear()
        self._model.setHorizontalHeaderLabels(self._HEADERS)

        for row in rows:
            status_code = int(row[5]) if row[5] is not None else 0
            status_item = QStandardItem(_STATUS_LABELS.get(status_code, "?"))
            status_item.setData(status_code, Qt.UserRole + 1)

            id_item = QStandardItem(str(row[0]))
            id_item.setData(row[0], Qt.UserRole)

            self._model.appendRow([
                status_item,
                id_item,
                QStandardItem(row[1] or ""),
                QStandardItem(str(row[2]) if row[2] else ""),
                QStandardItem(row[3] or ""),
                QStandardItem(row[4] or ""),
                QStandardItem(str(row[7])),
                QStandardItem(str(row[8])),
                QStandardItem(row[6] or ""),   # notes
            ])

        h = self._table.horizontalHeader()
        for i in range(4):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        for i in range(4, len(self._HEADERS)):
            h.setSectionResizeMode(i, QHeaderView.Stretch)

    def refresh(self):
        self.load_run_list()

    # ── Interaction ────────────────────────────────────────────────────────────

    def _get_selected_batch_id(self) -> int | None:
        idx = self._table.selectionModel().currentIndex()
        if not idx.isValid():
            QMessageBox.warning(self, "No Selection", "Please select a batch first.")
            return None
        item = self._model.item(idx.row(), 1)
        return item.data(Qt.UserRole) if item else None

    def _on_click(self, index):
        item = self._model.item(index.row(), 1)
        if item:
            bid = item.data(Qt.UserRole)
            if bid is not None:
                self.cmbSearchType.setCurrentText("Batch ID")
                self.txtSearch.setText(str(bid))

    def _on_double_click(self, _index):
        bid = self._get_selected_batch_id()
        if bid:
            self._open_batch_details(bid)

    def _on_search_type_changed(self, text: str):
        self.txtSearch.set_action_text(
            "Open Batch →" if text == "Batch ID" else "Search →"
        )

    def _on_search_or_open(self):
        if self.cmbSearchType.currentText() == "Batch ID":
            val = self.txtSearch.text().strip()
            if not val:
                self.load_run_list()
                return
            try:
                self._open_batch_details(int(val))
            except ValueError:
                QMessageBox.warning(self, "Invalid ID", "Batch ID must be a number.")
        else:
            self.load_run_list()

    def _reset_filters(self):
        self.txtSearch.clear()
        self.cmbSearchType.setCurrentIndex(0)
        self.cmbStatus.setCurrentIndex(0)
        self.cmbEquipFilter.setCurrentIndex(0)
        self.load_run_list()

    # ── Actions ────────────────────────────────────────────────────────────────

    def _create_batch(self):
        if PhysicalPrepCreateRunDialog is None:
            show_message(self, "Missing", "ams_physical_prep_create_run_gui not found.")
            return
        dlg = PhysicalPrepCreateRunDialog(self)
        if dlg.exec_() == QDialog.Accepted and dlg.new_batch_id:
            self.load_run_list()
            self._open_batch_details(dlg.new_batch_id)

    def _open_batch_details(self, batch_id: int):
        if PhysicalPrepRunDetailsWindow is None:
            show_message(self, "Missing",
                         "ams_physical_prep_run_details_gui not found.")
            return
        if self._details_window:
            try:
                self._details_window.close()
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        win = PhysicalPrepRunDetailsWindow(batch_id=batch_id, parent=self)
        self._details_window = win
        win.finished.connect(self._on_details_closed)
        win.show()

    def _on_details_closed(self, _result):
        self._reset_filters()

    def _delete_batch(self):
        bid = self._get_selected_batch_id()
        if not bid:
            return
        if QMessageBox.question(
            self, "Confirm Delete",
            f"Permanently delete Physical Prep Batch {bid} and all its samples?\n"
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) == QMessageBox.No:
            return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text(
                    "DELETE FROM ams.physicalprep_sample WHERE physicalpreprunid = :bid"
                ), {"bid": bid})
                conn.execute(text(
                    "DELETE FROM ams.physicalprep_run WHERE physicalpreprunid = :bid"
                ), {"bid": bid})
                conn.commit()
            self.load_run_list()
        except Exception as exc:
            log.error("Delete physicalprep_run %d: %s", bid, exc)
            show_message(self, "Error", str(exc), QMessageBox.Critical)

    def _close_module(self):
        if isinstance(self.parent(), QWidget):
            self.close()
        else:
            QCoreApplication.instance().quit()
