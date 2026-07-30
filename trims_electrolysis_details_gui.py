
"""
trims_electrolysis_details_gui.py — TRIMS electrolysis run details view for IsoWorks.
Provides TrimsElectrolysisDetailsWindow for viewing and editing the load list,
sample voltages, and metadata of an existing electrolysis run, with DYMO label support.
"""
from __future__ import annotations
import os
import sys
import logging
import getpass
from datetime import datetime
from typing import Optional, List, Tuple, Set
from PyQt5.QtCore import Qt, QEvent, QTimer, QSize
from PyQt5.QtGui import (
    QStandardItemModel, QStandardItem, QColor, QBrush, QDoubleValidator
)
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QStyle,
    QGroupBox, QLineEdit, QTextEdit, QDateTimeEdit, QComboBox,
    QPushButton, QTableView, QHeaderView, QStyledItemDelegate, QLabel,
    QAbstractItemView, QDialog, QGridLayout, QAbstractItemDelegate, QCheckBox
)
from sqlalchemy import text
from db_core import db_manager
from shared_utils import check_employee_privilege, set_status, get_current_user_id, normalize_login_name
from gui_utils import show_message
from balance_serial import BalanceSerialReader, load_balance_equipment, list_available_ports

from sample_label_gui import SampleLabelDialog, analysis_spec


# -------------------- Delegates --------------------
class ColumnHighlightDelegate(QStyledItemDelegate):
    """Paints background for target columns (blue) while preserving item-specific backgrounds."""
    def __init__(self, parent=None, target_columns=None, bg_color="#e3f2fd"):
        super().__init__(parent)
        self.target_columns = set(target_columns or [])
        self.bg_color = QColor(bg_color)
        self.focus_border_color = QColor("#0078d7")

    def set_target_columns(self, cols: List[int]):
        self.target_columns = set(cols)

    def paint(self, painter, option, index):
        # If an item-specific background exists, keep it.
        item_bg = index.data(Qt.BackgroundRole)

        painter.save()
        if item_bg and item_bg != QBrush():
            painter.fillRect(option.rect, item_bg)
        elif index.column() in self.target_columns:
            painter.fillRect(option.rect, self.bg_color)
        painter.restore()

        super().paint(painter, option, index)

        # Draw focus border if active and editable
        if (option.state & QStyle.State_HasFocus) and (index.flags() & Qt.ItemIsEditable):
            painter.save()
            pen = painter.pen()
            pen.setColor(self.focus_border_color)
            pen.setWidth(2)
            painter.setPen(pen)
            rect = option.rect.adjusted(1, 1, -1, -1)
            painter.drawRect(rect)
            painter.restore()


class NumericDelegate(ColumnHighlightDelegate):
    """Numeric editor that commits on Enter and advances to same column / next row."""
    def __init__(self, parent=None, decimals=1):
        super().__init__(parent)
        self.decimals = decimals  # 1 decimal

    def createEditor(self, parent, option, index):
        if index.column() not in self.target_columns:
            return None
        editor = QLineEdit(parent)
        editor.setValidator(QDoubleValidator(bottom=-1e12, top=1e12, decimals=self.decimals))
        editor.installEventFilter(self)
        return editor

    def setEditorData(self, editor, index):
        text = index.data(Qt.EditRole)
        editor.setText(text or "")
        editor.selectAll()

    def setModelData(self, editor, model, index):
        txt = editor.text().strip()
        if txt:
            try:
                val = float(txt)
                model.setData(index, f"{val:.{self.decimals}f}", Qt.EditRole)
                model.setData(index, val, Qt.UserRole)
            except ValueError:
                model.setData(index, txt, Qt.EditRole)
        else:
            model.setData(index, None, Qt.EditRole)

    def eventFilter(self, editor, event):
        if event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                self.commitData.emit(editor)
                self.closeEditor.emit(editor, QStyledItemDelegate.NoHint)
                view = self.parent()
                if isinstance(view, QTableView):
                    cur = view.currentIndex()
                    if cur.isValid():
                        row_count = view.model().rowCount()
                        next_row = (cur.row() + 1) % row_count
                        next_idx = view.model().index(next_row, cur.column())
                        view.setCurrentIndex(next_idx)
                        view.edit(next_idx)
                        view.scrollTo(next_idx)
                return True

            elif event.key() == Qt.Key_Escape:
                # Cancel edit without closing the dialog
                self.closeEditor.emit(editor, QAbstractItemDelegate.RevertModelCache)
                return True

        return super().eventFilter(editor, event)

class WFinalDelegate(QStyledItemDelegate):
    """Three-state W Final painter based on target mass:
       >= target -> green, >= 0.5*target -> yellow, else red; gray if no target/value."""
    def __init__(self, parent=None, target_mass=0.0):
        super().__init__(parent)
        self.target_mass = float(target_mass or 0.0)
        self._red = QColor("#ffcdd2")
        self._yellow = QColor("#fff9c4")
        self._green = QColor("#c8e6c9")
        self._gray = QColor("#f9f9f9")

    def set_target_mass(self, m: float):
        try:
            self.target_mass = float(m or 0.0)
        except Exception:
            self.target_mass = 0.0

    def paint(self, painter, option, index):
        # Parse displayed value
        val = None
        data = index.data(Qt.DisplayRole)
        try:
            s = str(data) if data is not None else ""
            val = float(s) if s.strip() != "" else None
        except Exception:
            val = None

        # Decide background color (three-state)
        if self.target_mass > 0 and val is not None:
            if val >= self.target_mass:
                bg = self._green
            elif val >= 0.5 * self.target_mass:
                bg = self._yellow
            else:
                bg = self._red
        else:
            bg = self._gray

        painter.save()
        painter.fillRect(option.rect, QBrush(bg))
        painter.restore()
        super().paint(painter, option, index)


class StatusComboDelegate(QStyledItemDelegate):
    """ComboBox delegate for Status with fixed options (4, 5, -9).
       Editability controlled by callback; model-only changes (persisted later when finishing)."""
    def __init__(self, parent=None, options: List[Tuple[int, str]] = None, can_edit_callback=None):
        super().__init__(parent)
        self.options = options or []  # list of (code, description)
        self.can_edit_callback = can_edit_callback or (lambda idx: False)

    def createEditor(self, parent, option, index):
        if not self.can_edit_callback(index):
            return None
        cb = QComboBox(parent)
        for code, desc in self.options:
            cb.addItem(desc, code)
        cb.activated.connect(lambda _: self.commitData.emit(cb))
        return cb

    def setEditorData(self, editor, index):
        current_code = index.data(Qt.UserRole)
        if current_code is None:
            current_code = 4
        i = editor.findData(current_code)
        editor.setCurrentIndex(max(0, i))

    def setModelData(self, editor, model, index):
        code = editor.currentData()
        desc = editor.currentText()
        model.setData(index, desc, Qt.DisplayRole)
        model.setData(index, code, Qt.UserRole)


# -------------------- Main Window --------------------
class TrimsElectrolysisDetailsWindow(QDialog):
    # Column Indices (reordered so Cancel? and Status appear before Remarks)
    COL_CELL = 0; COL_AID = 1; COL_SAMPLE = 2; COL_TYPE = 3
    COL_PRE_DIL = 4; COL_EMPTY = 5; COL_FULL_PRE = 6; COL_FULL_POST = 7
    COL_W_INIT = 8; COL_W_FINAL = 9; COL_NA2O2 = 10; COL_AMPH = 11
    COL_TRAP_PRE = 12; COL_TRAP_POST = 13
    COL_BOT_EMPTY = 14; COL_BOT_FULL = 15; COL_BOT_RESID = 16; COL_NEUT = 17

    COL_CANCEL = 18      # Electrolysis.IsIgnored (checkbox "Cancel?")
    COL_STATUS = 19      # Analysis.Status (combo or display)
    COL_REMARKS = 20     # Remarks LAST, so we can stretch it

    # Weight columns _on_item_changed() actually persists on edit (i.e. the
    # ones a balance reading can usefully land in) -- COL_NA2O2/COL_AMPH/
    # COL_NEUT are double-click-activatable but aren't in that handler's
    # column list, so editing them alone doesn't autosave; pre-existing gap,
    # left as-is, just excluded from balance targeting so a captured
    # reading is never silently lost.
    BALANCE_TARGET_COLS = {
        COL_EMPTY, COL_FULL_PRE, COL_FULL_POST,
        COL_TRAP_PRE, COL_TRAP_POST,
        COL_BOT_EMPTY, COL_BOT_FULL, COL_BOT_RESID,
    }

    def __init__(self, run_id: int, link_criteria: str = "", parent: Optional[QWidget] = None, compact_ui: bool = True) -> None:
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)
        self.run_id = run_id
        self.compact_ui = compact_ui
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowTitle(f"ElysRUN::{run_id}")
        self.resize(1400, 900)
        self.has_write_priv = False
        self._is_locked = False
        self.is_editing = False
        self.active_edit_cols: Set[int] = set()
        self.is_ntu_mode = False
        self._updating_model = False
        self.target_water_mass = 0.0  # Default

        # Lookup cache for statuses
        self.status_options: List[Tuple[int, str]] = []  # [(4,"Being Enriched"), (5,"Enriched"), (-9,"Failed")]
        self.status_desc_by_code = {}  # {code: desc}

        # --- UI Components ---
        self.btnEdit = QPushButton("Edit"); self.btnEdit.setAutoDefault(False)
        self.btnSave = QPushButton("Save"); self.btnSave.setAutoDefault(False)
        self.btnCancel = QPushButton("Cancel"); self.btnCancel.setAutoDefault(False)
        self.btnSave.hide(); self.btnCancel.hide()
        self.btnPrintLabels = QPushButton("Print Labels"); self.btnPrintLabels.setAutoDefault(False)
        self.btnClose = QPushButton("Close"); self.btnClose.setAutoDefault(False)
        self.txtRunID = QLineEdit(); self.dtStart = QDateTimeEdit()
        self.dtEnd = QDateTimeEdit(); self.chkFinished = QCheckBox("Finished?")
        self.dtStart.setCalendarPopup(True)
        self.dtEnd.setCalendarPopup(True)
        self.cmbTechnician1 = QComboBox(); self.cmbTechnician2 = QComboBox()
        self.cmbSystem = QComboBox(); self.txtProcedure = QLineEdit()
        self.txtRemarks = QTextEdit()

        # Table
        self.table = QTableView()
        self.model = QStandardItemModel()
        self.table.setModel(self.model)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)  # original behavior
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)  # locked by default

        # Delegates
        self.num_delegate = NumericDelegate(self.table, decimals=1)
        self.wfinal_delegate = WFinalDelegate(self.table, target_mass=self.target_water_mass)
        self.status_delegate = StatusComboDelegate(
            self.table,
            options=self.status_options,
            # can_edit_callback=self._can_edit_status_index
        )

        self.status_label = QLabel("Ready")

        # Balance link (RS-232/USB) -- bar only shown while editing, mirrors
        # web's TrimsElectrolysis.tsx isEditing-gated balance bar.
        self.cmbBalance = QComboBox()
        self.cmbBalance.setMinimumWidth(160)
        self.cmbPort = QComboBox()
        self.cmbPort.setMinimumWidth(160)
        self.btnBalanceRefreshPorts = QPushButton("⟳")
        self.btnBalanceRefreshPorts.setFixedWidth(28)
        self.btnBalanceRefreshPorts.setToolTip("Refresh serial port list")
        self.btnBalanceConnect = QPushButton("Connect")
        self.lblBalanceDot = QLabel("●")
        self.lblBalanceDot.setStyleSheet("color:#B0BEC5; font-size:14px;")
        self.lblBalanceReading = QLabel("")
        self.lblBalanceReading.setStyleSheet("color:#555; font-family:monospace;")
        self.balance_reader = BalanceSerialReader(self)
        self.balance_reader.stableReading.connect(self._on_balance_reading)
        self.balance_reader.statusChanged.connect(self._on_balance_status)
        self.balance_reader.connectionChanged.connect(self._on_balance_connection_changed)

        self._init_ui()
        try:
            self._check_privileges()
            self._load_lookups()
            self._load_header()
            # Update delegates with lookup-dependent values
            self.wfinal_delegate.set_target_mass(self.target_water_mass)
            self.status_delegate.options = self.status_options
            self._load_details()
            self._update_column_visibility()
            self._apply_read_only_state()
        except Exception as e:
            logging.error(f"Init failed: {e}")
            self.status_label.setText(f"Error: {e}")

    # ---------- UI scaffolding ----------
    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        top_bar = QHBoxLayout()
        top_bar.addStretch()
        top_bar.addWidget(self.btnEdit)
        top_bar.addWidget(self.btnSave)
        top_bar.addWidget(self.btnCancel)
        top_bar.addWidget(self.btnPrintLabels)
        top_bar.addWidget(self.btnClose)
        main_layout.addLayout(top_bar)

        self.balance_bar_w = QWidget()
        balance_bar = QHBoxLayout(self.balance_bar_w)
        balance_bar.setContentsMargins(0, 0, 0, 4)
        balance_bar.addWidget(QLabel("<b>Balance:</b>"))
        balance_bar.addWidget(self.cmbBalance)
        balance_bar.addWidget(self.cmbPort)
        balance_bar.addWidget(self.btnBalanceRefreshPorts)
        balance_bar.addWidget(self.btnBalanceConnect)
        balance_bar.addWidget(self.lblBalanceDot)
        balance_bar.addWidget(self.lblBalanceReading)
        balance_bar.addStretch(1)
        self.balance_bar_w.setVisible(False)
        main_layout.addWidget(self.balance_bar_w)

        self._build_header_form()
        main_layout.addWidget(self.header_group)

        self._apply_table_styles()
        main_layout.addWidget(self.table, 1)
        main_layout.addWidget(self.status_label)

        self.btnEdit.clicked.connect(self._start_edit_mode)
        self.btnSave.clicked.connect(self._save_edit_mode)
        self.btnCancel.clicked.connect(self._cancel_edit_mode)
        self.btnPrintLabels.clicked.connect(self._print_electrolysis_labels)
        self.btnClose.clicked.connect(self.accept)
        self.model.itemChanged.connect(self._on_item_changed)
        self.table.horizontalHeader().sectionDoubleClicked.connect(self._on_header_double_clicked)
        self.chkFinished.toggled.connect(self._on_finished_toggled)
        self.btnBalanceRefreshPorts.clicked.connect(self._refresh_balance_ports)
        self.btnBalanceConnect.clicked.connect(self._toggle_balance_connect)

    def _print_electrolysis_labels(self):
        """Fetch cells for this run and open the QR label printer."""
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT e.CellID,
                           s.Prefix, s.SampleID, s.sName,
                           a.AnalysisID
                    FROM   TRIMS.Electrolysis e
                    JOIN   Analysis a ON a.AnalysisID = e.AnalysisID
                    JOIN   Sample   s ON s.SampleID   = a.SampleID
                                     AND s.Prefix     = a.Prefix
                    WHERE  e.RunID = :r
                    ORDER  BY e.CellID
                """), {"r": self.run_id}).fetchall()
        except Exception as exc:
            logging.error("Electrolysis label fetch: %s", exc)
            show_message(self, "Error", str(exc))
            return
        if not rows:
            show_message(self, "No Data", "No cells found for this run.")
            return
        specs = [
            analysis_spec(
                prefix=r.Prefix or "",
                sample_id=str(r.SampleID),
                analysis_id=str(r.AnalysisID),
                container_num=f"Cell {r.CellID}",
                job_type="Electrolysis",
            )
            for r in rows
        ]
        SampleLabelDialog.show_batch(specs, parent=self).exec_()

    def _build_header_form(self):
        self.header_group = QGroupBox("Run Metadata")
        self.header_group.setStyleSheet("""
        QGroupBox { background-color: #FAFAFC; border: 1px solid #D9D9E3; border-radius: 8px; margin-top: 8px; font-weight: 600; padding-top: 10px; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #2D2D33; font-size: 13px; }
        QLabel.header-label { color: #3A3A44; font-size: 12.5px; font-weight: 500; }
        QLineEdit, QComboBox, QTextEdit, QDateTimeEdit { background: #FFFFFF; border: 1px solid #D0D0DD; border-radius: 6px; padding: 4px 6px; font-size: 12.5px; }
        QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QDateTimeEdit:focus { border: 1px solid #4C82FF; outline: none; }
        QDateTimeEdit::drop-down { background: #C5CAE9; border: none; width: 22px; border-radius: 0 6px 6px 0; }
        QDateTimeEdit::drop-down:hover { background: #7986CB; }
        QDateTimeEdit::drop-down:pressed { background: #3949AB; }
        QDateTimeEdit::down-arrow { image: none; width: 0; height: 0; border-left: 5px solid transparent; border-right: 5px solid transparent; border-top: 6px solid #3949AB; }
        """)
        grid = QGridLayout()
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setColumnStretch(5, 1)

        def lbl(text):
            l = QLabel(text); l.setProperty("class", "header-label"); l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return l

        self.txtRunID.setReadOnly(True); self.txtRunID.setFixedWidth(100)
        self.txtRunID.setStyleSheet("background-color: #F2F3F7; font-weight: 600;")
        self.cmbSystem.setEnabled(False); self.txtProcedure.setReadOnly(True)
        self.dtStart.setDisplayFormat("yyyy-MM-dd HH:mm"); self.dtEnd.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.txtRemarks.setMaximumHeight(50)

        grid.addWidget(lbl("Run ID:"), 0, 0); grid.addWidget(self.txtRunID, 0, 1)
        grid.addWidget(lbl("Start:"), 1, 0); grid.addWidget(self.dtStart, 1, 1)
        grid.addWidget(lbl("Tech 1:"), 2, 0); grid.addWidget(self.cmbTechnician1, 2, 1)
        grid.addWidget(lbl("System:"), 0, 2); grid.addWidget(self.cmbSystem, 0, 3)

        h_end = QHBoxLayout(); h_end.setSpacing(3)
        h_end.addWidget(self.dtEnd); h_end.addWidget(self.chkFinished)
        grid.addWidget(lbl("End:"), 1, 2); grid.addLayout(h_end, 1, 3)

        grid.addWidget(lbl("Tech 2:"), 2, 2); grid.addWidget(self.cmbTechnician2, 2, 3)
        grid.addWidget(lbl("Procedure:"), 0, 4); grid.addWidget(self.txtProcedure, 0, 5)
        grid.addWidget(lbl("Remarks:"), 3, 0); grid.addWidget(self.txtRemarks, 3, 1, 1, 5)

        self.header_group.setLayout(grid)

    def _apply_table_styles(self):
        self.table.setStyleSheet("""
        QTableView { border: 1px solid #d0d0d0; background-color: white; gridline-color: #cccccc; selection-background-color: #DDEEFF; selection-color: #000000; }
        QTableView::item { padding: 4px; border: none; }
        QHeaderView::section { background-color: #f0f0f0; font-weight: bold; border: 1px solid #d0d0d0; padding: 4px; }
        """)
        self.table.setShowGrid(True)
        self.table.setAlternatingRowColors(False)

    # ---------- Data loading ----------
    def _check_privileges(self):
        try:
            self.has_write_priv = check_employee_privilege(getpass.getuser(), "AccessEnrichment")
        except Exception:
            self.has_write_priv = False

    def _load_lookups(self):
        with db_manager.get_connection() as conn:
            self.cmbTechnician1.addItem("", None); self.cmbTechnician2.addItem("", None)
            for r in conn.execute(text("SELECT EmployeeID, LastName, FirstMiddleName FROM Employee ORDER BY LastName")):
                name = f"{r.LastName}, {r.FirstMiddleName}"
                self.cmbTechnician1.addItem(name, r.EmployeeID)
                self.cmbTechnician2.addItem(name, r.EmployeeID)
            for r in conn.execute(text("SELECT EquipmentID, EquipmentName FROM Equipment WHERE CategoryID=2 ORDER BY EquipmentID")):
                self.cmbSystem.addItem(r.EquipmentName, r.EquipmentID)

            # Status lookup (4, 5, -9) for the combo
            st_rows = conn.execute(text(
                "SELECT Status, Description FROM StatusLookup WHERE Status IN (4,5,-9) ORDER BY Status"
            )).fetchall()
            self.status_options = [(int(r.Status), str(r.Description)) for r in st_rows]
            self.status_desc_by_code = {int(r.Status): str(r.Description) for r in st_rows}

        for eid, name in load_balance_equipment():
            self.cmbBalance.addItem(name, eid)

    def _load_header(self):
        with db_manager.get_connection() as conn:
            sql = text("""
                SELECT r.*, p.ProcedureName, es.IsCellSizeFlexible, ep.TargetWaterMass
                FROM TRIMS.ElectrolysisRun r
                LEFT JOIN AnalysisProcedure p ON r.ProcedureID = p.ProcedureID
                LEFT JOIN EnrichmentProcedure ep ON r.ProcedureID = ep.ProcedureID
                LEFT JOIN ElectrolysisSystem es ON r.ElysSystemID = es.ElysSystemID
                WHERE r.RunID = :rid
            """)
            row = conn.execute(sql, {"rid": self.run_id}).fetchone()
            if not row:
                set_status(self.status_label, "Run not found.", "error")
                return

            self.txtRunID.setText(str(row.RunID))
            self.txtProcedure.setText(row.ProcedureName or str(row.ProcedureID))

            if row.RunStartTime:
                self.dtStart.setDateTime(row.RunStartTime)
            if row.RunEndTime:
                self.dtEnd.setDateTime(row.RunEndTime)
                self.chkFinished.setChecked(True)
            else:
                self.dtEnd.setDateTime(datetime.now())
                self.chkFinished.setChecked(False)
                self.dtEnd.setEnabled(False)

            self._set_combo(self.cmbTechnician1, row.TechnicianID)
            try:
                self._set_combo(self.cmbTechnician2, row.Technician2)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            self._set_combo(self.cmbSystem, row.ElysSystemID)

            self.txtRemarks.setPlainText(row.Remarks or "")
            self._is_locked = bool(getattr(row, 'IsLocked', False))
            if row.TargetWaterMass:
                self.target_water_mass = float(row.TargetWaterMass)
            self.is_ntu_mode = bool(row.IsCellSizeFlexible)

            self.setWindowTitle(f"ElysRUN::{self.run_id} [{'NTU' if self.is_ntu_mode else 'Classic'}]")

    def _set_combo(self, cmb, val):
        if val is None:
            cmb.setCurrentIndex(0)
            return
        idx = cmb.findData(val)
        if idx >= 0:
            cmb.setCurrentIndex(idx)

    def _load_details(self):
        self._updating_model = True
        try:
            with db_manager.get_connection() as conn:
                sql = text(
                    "SELECT e.*, s.sName, s.Prefix, a.SampleID as SID, "
                    "a.Status AS AStatus, "                # Analysis.Status
                    "e.IsIgnored AS IsIgnored, "           # Electrolysis.IsIgnored
                    "s.SampleType as ActualSampleType "
                    "FROM TRIMS.Electrolysis e "
                    "JOIN Analysis a ON e.AnalysisID = a.AnalysisID "
                    "JOIN Sample s ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix "
                    "WHERE e.RunID = :rid ORDER BY e.CellID"
                )
                rows = conn.execute(sql, {"rid": self.run_id}).fetchall()

                self.model.clear()
                headers = [
                    "Cell", "AnalysisID", "Sample", "Type",
                    "Pre-Dil", "Empty", "Full Pre", "Full Post",
                    "W Init", "W Final", "Na2O2", "AmpHr",
                    "Trap Pre", "Trap Post",
                    "Bot Empty", "Bot Full", "Bot Resid", "Neut",
                    "Cancel?", "Status", "Remarks"   # Cancel, Status, then Remarks
                ]
                self.model.setHorizontalHeaderLabels(headers)

                brush_ro = QBrush(QColor("#f9f9f9"))
                for r in rows:
                    w_init, w_final = self._calc_weights(r)
                    a_status = int(getattr(r, "AStatus", 4) or 4)
                    status_desc = self.status_desc_by_code.get(a_status, str(a_status))
                    is_ignored = bool(getattr(r, "IsIgnored", False))

                    items = [
                        QStandardItem(str(r.CellID)),
                        QStandardItem(str(r.AnalysisID)),
                        QStandardItem(f"{r.Prefix}-{r.SID} {r.sName}"),
                        QStandardItem(str(r.ActualSampleType)),
                        QStandardItem(self._fmt(r.DiluentPreEnrichment)),
                        QStandardItem(self._fmt(r.MassEmptyCell)),
                        QStandardItem(self._fmt(r.FullCellMassBefore)),
                        QStandardItem(self._fmt(r.FullCellMassAfter)),
                        QStandardItem(f"{w_init:.1f}"),
                        QStandardItem(f"{w_final:.1f}"),
                        QStandardItem(self._fmt(r.Na2O2Mass)),
                        QStandardItem(self._fmt(r.AmpereHour)),
                        QStandardItem(self._fmt(r.ColdTrapMassBefore)),
                        QStandardItem(self._fmt(r.ColdTrapMassAfter)),
                        QStandardItem(self._fmt(r.EmptyBottleMass)),
                        QStandardItem(self._fmt(r.FullBottleMassBefore)),
                        QStandardItem(self._fmt(r.FullBottleMassAfter)),
                        QStandardItem(self._fmt(r.CellMassAfterNeutralization)),

                        QStandardItem(""),               # Cancel? (checkbox)
                        QStandardItem(status_desc),      # Status (desc display; UserRole = code)
                        QStandardItem(r.Remarks or "")   # Remarks (plain text)
                    ]
                    # Keys / roles
                    items[self.COL_CELL].setData(r.CellID, Qt.UserRole)
                    items[self.COL_STATUS].setData(a_status, Qt.UserRole)

                    # Read-only columns get a light gray background, EXCEPT W Final (delegate paints)
                    for i in [self.COL_CELL, self.COL_AID, self.COL_SAMPLE, self.COL_TYPE, self.COL_W_INIT]:
                        items[i].setEditable(False)
                        items[i].setBackground(brush_ro)
                        items[i].setFlags(items[i].flags() & ~Qt.ItemIsEditable)

                    # W Final read-only (delegate will color)
                    items[self.COL_W_FINAL].setEditable(False)
                    items[self.COL_W_FINAL].setFlags(items[self.COL_W_FINAL].flags() & ~Qt.ItemIsEditable)

                    # Cancel? checkbox (gated by Edit Mode later)
                    cancel_item = items[self.COL_CANCEL]
                    cancel_item.setCheckable(True)
                    cancel_item.setCheckState(Qt.Checked if is_ignored else Qt.Unchecked)
                    cancel_item.setFlags(
                        (cancel_item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                        & ~Qt.ItemIsEditable
                    )

                    # Status column: delegate controls editability per row; keep selectable
                    items[self.COL_STATUS].setEditable(True)

                    # Remarks is text; editability controlled by Edit Mode

                    self.model.appendRow(items)

                # Delegates
                self.table.setItemDelegateForColumn(self.COL_W_FINAL, self.wfinal_delegate)
                self.table.setItemDelegateForColumn(self.COL_STATUS, self.status_delegate)

                # Restore the "perfect earlier" widths, then make only Remarks auto-resize
                self.table.resizeColumnsToContents()
                self.table.horizontalHeader().setSectionResizeMode(self.COL_REMARKS, QHeaderView.Stretch)

                # Wrapping & row sizing for better Remarks visibility
                self.table.setWordWrap(True)
                self.table.resizeRowsToContents()

        finally:
            self._updating_model = False
        # Ensure per-row editability in sync
        self._update_editable_columns()

    # ---------- Calculations & helpers ----------
    def _calc_weights(self, r):
        def g(attr):
            val = getattr(r, attr, 0)
            return float(val) if val else 0.0

        if self.is_ntu_mode and getattr(r, "FullBottleMassBefore", None):
            w_init = g("FullBottleMassBefore") - g("EmptyBottleMass")
        else:
            w_init = g("FullCellMassBefore") - g("MassEmptyCell")

        if getattr(r, "FullCellMassAfter", None) is None:
            w_final = g("ColdTrapMassAfter") - g("ColdTrapMassBefore")
        else:
            w_final = g("FullCellMassAfter") - g("MassEmptyCell")

        return w_init, w_final

    def _has_w_final_row(self, r: int) -> bool:
        """
        Returns True if the row has any user input indicating W_Final presence:
        either Full Post or Trap Post has non-empty text.
        """
        def has_text(idx: int) -> bool:
            it = self.model.item(r, idx)
            if it is None:
                return False
            t = it.text()
            return bool(t and t.strip())
        return has_text(self.COL_FULL_POST) or has_text(self.COL_TRAP_POST)

    def _is_row_ignored(self, r: int) -> bool:
        """Returns True if the 'Cancel?' checkbox is checked for row r."""
        it = self.model.item(r, self.COL_CANCEL)
        return bool(it and it.checkState() == Qt.Checked)

    def _update_column_visibility(self):
        for c in [self.COL_BOT_EMPTY, self.COL_BOT_FULL, self.COL_BOT_RESID, self.COL_NEUT]:
            self.table.setColumnHidden(c, not self.is_ntu_mode)

    def _fmt(self, val):
        return f"{val:.1f}" if val is not None else ""

    # ---------- Edit mode ----------
    def _on_finished_toggled(self, checked):
        if not self.is_editing:
            return
        self.dtEnd.setEnabled(checked)
        if checked and self.dtEnd.dateTime().toPyDateTime() < datetime(2000, 1, 1):
            self.dtEnd.setDateTime(datetime.now())

    def _start_edit_mode(self):
        if not self.has_write_priv or self._is_locked:
            return
        self.is_editing = True
        self._apply_edit_state(True)
        self.btnEdit.hide()
        self.btnSave.show()
        self.btnCancel.show()
        self.table.setEditTriggers(QAbstractItemView.AllEditTriggers)
        set_status(self.status_label, "Edit Mode. Double-click Header to unlock numeric columns.", "processing")
        self.active_edit_cols = {self.COL_EMPTY}
        self.num_delegate.set_target_columns(list(self.active_edit_cols))
        self._update_editable_columns()
        self._refresh_balance_ports()
        self.balance_bar_w.setVisible(True)

    def _save_edit_mode(self):
        self._save_header()
        self.is_editing = False
        self.btnSave.hide()
        self.btnCancel.hide()
        self.btnEdit.show()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        set_status(self.status_label, "Read Only", "neutral")
        self.active_edit_cols.clear()
        self._update_editable_columns()
        self._apply_read_only_state()
        self.balance_reader.disconnect_port()
        self.balance_bar_w.setVisible(False)

    def _cancel_edit_mode(self):
        self.is_editing = False
        self._load_header()
        self._load_details()
        self._update_column_visibility()
        self.btnSave.hide()
        self.btnCancel.hide()
        self.btnEdit.show()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        set_status(self.status_label, "Read Only", "neutral")
        self.active_edit_cols.clear()
        self._update_editable_columns()
        self._apply_read_only_state()
        self.balance_reader.disconnect_port()
        self.balance_bar_w.setVisible(False)

    # ---------- Balance link (RS-232/USB) ----------
    def _refresh_balance_ports(self):
        current = self.cmbPort.currentText()
        self.cmbPort.clear()
        ports = list_available_ports()
        self.cmbPort.addItems(ports)
        idx = self.cmbPort.findText(current)
        if idx >= 0:
            self.cmbPort.setCurrentIndex(idx)

    def _toggle_balance_connect(self):
        if self.balance_reader.connected:
            self.balance_reader.disconnect_port()
            return
        equipment_id = self.cmbBalance.currentData()
        port_name = self.cmbPort.currentText()
        if not equipment_id or not port_name:
            show_message(self, "Balance", "Select a balance and a serial port first.")
            return
        self.balance_reader.connect_to(equipment_id, port_name)

    def _on_balance_connection_changed(self, connected: bool):
        self.btnBalanceConnect.setText("Disconnect" if connected else "Connect")
        self.lblBalanceDot.setStyleSheet(f"color:{'#43A047' if connected else '#B0BEC5'}; font-size:14px;")
        self.cmbBalance.setEnabled(not connected)
        self.cmbPort.setEnabled(not connected)
        self.btnBalanceRefreshPorts.setEnabled(not connected)

    def _on_balance_status(self, text: str):
        self.lblBalanceReading.setText(text)

    def _on_balance_reading(self, value: float):
        """Inject a stable balance reading into the currently-targeted cell
        (current row, the single active edit column) and advance to the next
        row -- mirrors web's injectBalanceValue()/advanceFocus(). Writing via
        setText() alone triggers the existing itemChanged -> _save_row_to_db
        autosave, so no separate commit call is needed here."""
        if not self.is_editing or len(self.active_edit_cols) != 1:
            return
        col = next(iter(self.active_edit_cols))
        if col not in self.BALANCE_TARGET_COLS:
            return
        row = self.table.currentIndex().row()
        if row < 0:
            row = 0
        if row >= self.model.rowCount():
            return

        self.model.item(row, col).setText(f"{value:.1f}")

        next_row = row + 1
        if next_row < self.model.rowCount():
            self.table.setCurrentIndex(self.model.index(next_row, col))
            self.table.scrollTo(self.model.index(next_row, col))

    def closeEvent(self, event):
        self.balance_reader.disconnect_port()
        super().closeEvent(event)

    def _on_header_double_clicked(self, index):
        if not self.is_editing:
            return
        # Prevent activating read-only or non-numeric columns
        if index in [self.COL_CELL, self.COL_AID, self.COL_SAMPLE, self.COL_TYPE,
                     self.COL_W_INIT, self.COL_W_FINAL, self.COL_STATUS, self.COL_CANCEL, self.COL_REMARKS]:
            return

        # Exclusively unlock the clicked numeric column
        self.active_edit_cols = {index}
        self.num_delegate.set_target_columns(list(self.active_edit_cols))
        self._update_editable_columns()
        set_status(self.status_label, f"Active: {self.model.headerData(index, Qt.Horizontal)}", "processing")

    def _update_editable_columns(self):
        """Assign numeric delegate only to unlocked columns; Status model-editable per-row; Cancel? gated by Edit Mode; Remarks editable in Edit Mode."""
        self.model.blockSignals(True)
        try:
            # Assign delegates
            for c in range(self.model.columnCount()):
                if c == self.COL_W_FINAL:
                    self.table.setItemDelegateForColumn(c, self.wfinal_delegate)
                elif c == self.COL_STATUS:
                    self.table.setItemDelegateForColumn(c, self.status_delegate)
                elif c in [self.COL_REMARKS, self.COL_CANCEL]:
                    self.table.setItemDelegateForColumn(c, None)  # default editors
                else:
                    if c in self.active_edit_cols:
                        self.table.setItemDelegateForColumn(c, self.num_delegate)
                    else:
                        self.table.setItemDelegateForColumn(c, None)

            # Update flags per cell
            for r in range(self.model.rowCount()):
                # Status editability driven by w_final presence and current Analysis.Status (<6)
                w_final_text = self.model.item(r, self.COL_W_FINAL).text()
                has_w_final = bool(w_final_text and w_final_text.strip())
                st_item = self.model.item(r, self.COL_STATUS)
                st_code = st_item.data(Qt.UserRole)
                st_code = st_code if st_code is not None else 4
                status_editable = self.is_editing and has_w_final and (st_code < 6)

                for c in range(self.model.columnCount()):
                    it = self.model.item(r, c)
                    if c in [self.COL_CELL, self.COL_AID, self.COL_SAMPLE, self.COL_TYPE, self.COL_W_INIT, self.COL_W_FINAL]:
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                        continue

                    if c == self.COL_REMARKS:
                        if self.is_editing:
                            it.setFlags(it.flags() | Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                        else:
                            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                        continue

                    if c == self.COL_CANCEL:
                        if self.is_editing:
                            it.setFlags(
                                (it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                                & ~Qt.ItemIsEditable
                            )
                        else:
                            it.setFlags(it.flags() & ~Qt.ItemIsUserCheckable & ~Qt.ItemIsEditable)
                        continue

                    if c == self.COL_STATUS:
                        if status_editable:
                            it.setFlags(it.flags() | Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                        else:
                            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                        continue

                    # Other numeric cells: editable only if their column is active
                    if c in self.active_edit_cols:
                        it.setFlags(it.flags() | Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    else:
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
        finally:
            self.model.blockSignals(False)
        self.table.viewport().update()

    def _apply_read_only_state(self):
        self.header_group.setEnabled(True)
        for w in [self.dtStart, self.dtEnd, self.txtRemarks, self.chkFinished]:
            if hasattr(w, 'setReadOnly'):
                w.setReadOnly(True)
            else:
                w.setEnabled(False)
        for c in [self.cmbTechnician1, self.cmbTechnician2]:
            c.setEnabled(False)
        self.btnEdit.setEnabled(self.has_write_priv and not self._is_locked)
        self.btnEdit.show()
        self.btnSave.hide()
        self.btnCancel.hide()

    def _apply_edit_state(self, editing):
        self.header_group.setEnabled(True)
        ro = not editing
        for w in [self.dtStart, self.dtEnd, self.txtRemarks]:
            w.setReadOnly(ro)
        self.chkFinished.setEnabled(editing)
        if editing:
            self._on_finished_toggled(self.chkFinished.isChecked())
        for c in [self.cmbTechnician1, self.cmbTechnician2]:
            c.setEnabled(editing)

    # ---------- Item changed & persistence ----------
    def _on_item_changed(self, item):
        if not self.is_editing or self._updating_model:
            return

        self._updating_model = True
        try:
            r = item.row(); c = item.column()

            # Handle numeric recalcs and Electrolysis persistence
            if c in [self.COL_EMPTY, self.COL_FULL_PRE, self.COL_FULL_POST,
                     self.COL_TRAP_PRE, self.COL_TRAP_POST,
                     self.COL_BOT_EMPTY, self.COL_BOT_FULL, self.COL_BOT_RESID]:
                try:
                    def g(idx):
                        t = self.model.item(r, idx).text()
                        return float(t) if t and t.strip() else 0.0

                    # Recalculate W Init / W Final
                    if self.is_ntu_mode and g(self.COL_BOT_FULL) > 0:
                        w_init = g(self.COL_BOT_FULL) - g(self.COL_BOT_EMPTY)
                    else:
                        w_init = g(self.COL_FULL_PRE) - g(self.COL_EMPTY)

                    if g(self.COL_FULL_POST) == 0:
                        w_final = g(self.COL_TRAP_POST) - g(self.COL_TRAP_PRE)
                    else:
                        w_final = g(self.COL_FULL_POST) - g(self.COL_EMPTY)

                    self.model.item(r, self.COL_W_INIT).setText(f"{w_init:.1f}")
                    self.model.item(r, self.COL_W_FINAL).setText(f"{w_final:.1f}")
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")

                # Recompute Status editability (depends on w_final presence)
                self._update_editable_columns()

                # Save numeric row (Electrolysis fields)
                self._save_row_to_db(r)

            elif c == self.COL_CANCEL:
                # Persist IsIgnored immediately
                self._save_row_to_db(r)
                # If now checked, jump to Remarks and open editor
                if item.checkState() == Qt.Checked:
                    remarks_idx = self.model.index(r, self.COL_REMARKS)
                    self.table.setCurrentIndex(remarks_idx)
                    self.table.scrollTo(remarks_idx)
                    if self.is_editing:
                        QTimer.singleShot(0, lambda: self.table.edit(remarks_idx))

            elif c == self.COL_STATUS:
                # Model-only change; DO NOT persist here (persist happens when finishing the run)
                pass

            elif c == self.COL_REMARKS:
                # Persist remarks inside _save_row_to_db
                self._save_row_to_db(r)

        finally:
            self._updating_model = False

    # ---------- Header save & run-level status propagation ----------
    def _save_header(self):
        if not self.has_write_priv:
            return

        end_date = self.dtEnd.dateTime().toPyDateTime() if self.chkFinished.isChecked() else None
        rem_text = self.txtRemarks.toPlainText()[:255]
        run_status = 5 if end_date else 4  # finished => 5 ("Enriched"), else 4 ("Being Enriched")

        try:
            with db_manager.get_connection() as conn:
                conn.execute(text(f"""
                    UPDATE TRIMS.ElectrolysisRun
                    SET RunStartTime=:s, RunEndTime=:e, TechnicianID=:t1, Technician2=:t2,
                        Remarks=:r, RunStatus=:st
                    WHERE RunID=:rid
                """), {
                    "s": self.dtStart.dateTime().toPyDateTime(), "e": end_date,
                    "t1": self.cmbTechnician1.currentData(), "t2": self.cmbTechnician2.currentData(),
                    "r": rem_text, "st": run_status, "rid": int(self.run_id)
                })
                conn.commit()
                set_status(self.status_label, "Metadata saved.", "success")

            # >>> Only when finishing the run, propagate Analysis.Status <<<
            if end_date:
                self._save_status_changes_enrichment(run_status)
                self._prompt_siam_alert()

        except Exception as e:
            logging.error(f"Header save error: {e}")
            set_status(self.status_label, f"Metadata Error: {e}", "error")

    def _save_status_changes_enrichment(self, run_status: int):
        """
        Transactionally set Analysis.Status for all rows in this run:
          - If Cancel? (IsIgnored) => Status = -9
          - Else, if W_Final present => Status = run_status
          - Else => Status = 4
        """
        try:
            with db_manager.get_connection() as conn:
                trans = conn.begin()  # start transaction
                try:
                    rows = conn.execute(text("""
                        SELECT a.AnalysisID, a.Status AS AStatus
                        FROM Analysis a
                        INNER JOIN TRIMS.Electrolysis e ON a.AnalysisID = e.AnalysisID
                        WHERE e.RunID = :rid
                        ORDER BY e.CellID
                    """), {"rid": int(self.run_id)}).fetchall()

                    conflicts = 0
                    for rr in rows:
                        if int(rr.AStatus or 0) > 5:
                            conflicts += 1

                    for r in range(self.model.rowCount()):
                        aid_text = self.model.item(r, self.COL_AID).text()
                        analysis_id = int(aid_text)
                        is_ignored = self._is_row_ignored(r)
                        has_w_final = self._has_w_final_row(r)

                        if is_ignored:
                            new_status = -9
                        elif has_w_final:
                            new_status = int(run_status)
                        else:
                            new_status = 4

                        conn.execute(text("""
                            UPDATE Analysis
                            SET Status = :st
                            WHERE AnalysisID = :aid
                        """), {"st": new_status, "aid": analysis_id})

                        # Reflect in the model (display + UserRole)
                        desc = self.status_desc_by_code.get(new_status, str(new_status))
                        st_item = self.model.item(r, self.COL_STATUS)
                        st_item.setText(desc)
                        st_item.setData(new_status, Qt.UserRole)

                    trans.commit()
                    if conflicts > 0:
                        set_status(self.status_label,
                                   f"Run finished. Status updated. Note: {conflicts} analyses had Status > 5.",
                                   "processing")
                    else:
                        set_status(self.status_label, "Run finished. Analysis statuses updated.", "success")
                except Exception:
                    trans.rollback()
                    raise
        except Exception as e:
            logging.error(f"SaveStatusChangesEnrichment error: {e}")
            set_status(self.status_label, f"Status propagation failed: {e}", "error")

    # ---------- Row persistence (Electrolysis) ----------
    def _save_row_to_db(self, r):
        try:
            cell_id = self.model.item(r, self.COL_CELL).data(Qt.UserRole)

            def g(c):
                t = self.model.item(r, c).text()
                return float(t) if t and t.strip() else None

            # Cancel? checkbox state -> IsIgnored (bool)
            cancel_item = self.model.item(r, self.COL_CANCEL)
            is_ignored = (cancel_item.checkState() == Qt.Checked)

            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE TRIMS.Electrolysis
                    SET DiluentPreEnrichment=:dil,
                        MassEmptyCell=:e, FullCellMassBefore=:pre, FullCellMassAfter=:post,
                        Na2O2Mass=:na, AmpereHour=:am,
                        ColdTrapMassBefore=:tp, ColdTrapMassAfter=:tpo,
                        EmptyBottleMass=:be, FullBottleMassBefore=:bf, FullBottleMassAfter=:br,
                        CellMassAfterNeutralization=:nm, Remarks=:rem,
                        IsIgnored=:ign
                    WHERE RunID=:rid AND CellID=:cell
                """), {
                    "rid": int(self.run_id), "cell": cell_id,
                    "dil": g(self.COL_PRE_DIL),
                    "e": g(self.COL_EMPTY), "pre": g(self.COL_FULL_PRE), "post": g(self.COL_FULL_POST),
                    "na": g(self.COL_NA2O2), "am": g(self.COL_AMPH),
                    "tp": g(self.COL_TRAP_PRE), "tpo": g(self.COL_TRAP_POST),
                    "be": g(self.COL_BOT_EMPTY), "bf": g(self.COL_BOT_FULL), "br": g(self.COL_BOT_RESID),
                    "nm": g(self.COL_NEUT), "rem": self.model.item(r, self.COL_REMARKS).text(),
                    "ign": is_ignored
                })
                conn.commit()
                set_status(self.status_label, f"Cell {cell_id} updated.", "success")
        except Exception as e:
            logging.error(f"Row save error: {e}")
            set_status(self.status_label, f"Save Error Cell {cell_id}", "error")

    def _prompt_siam_alert(self):
        # 1. Get active employees
        active_employees = []
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT EmployeeID, LastName, FirstMiddleName FROM Employee WHERE IsObsolete = FALSE ORDER BY LastName")).fetchall()
                active_employees = [(r.EmployeeID, f"{r.LastName}, {r.FirstMiddleName}") for r in res]
        except Exception as e:
            logging.error(f"Error querying active employees for SIAM alert: {e}")
            return

        if not active_employees:
            return

        # 2. Find sender ID (current logged in user)
        sender_id = None
        try:
            username = normalize_login_name(get_current_user_id())
            with db_manager.get_connection() as conn:
                row = conn.execute(text("SELECT EmployeeID FROM Employee WHERE LOWER(SystemLoginName) = :usr"), {"usr": username.lower()}).fetchone()
                if row:
                    sender_id = row.EmployeeID
        except Exception as e:
            logging.error(f"Error resolving sender employee ID: {e}")

        if not sender_id:
            # Fall back to self.cmbTechnician1's current selection if available
            sender_id = self.cmbTechnician1.currentData()
            if not sender_id:
                # If still not found, search for any non-obsolete employee
                sender_id = active_employees[0][0]

        # 3. Create dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Alert SIAM Technician")
        dialog.setMinimumWidth(400)
        layout = QVBoxLayout(dialog)

        info_lbl = QLabel(
            "This run has been finished. Would you like to notify a SIAM technician "
            "that pre-/post-enriched samples are ready for deuterium analysis?",
            dialog
        )
        info_lbl.setWordWrap(True)
        layout.addWidget(info_lbl)

        # Recipient combobox
        recip_layout = QHBoxLayout()
        recip_lbl = QLabel("Recipient:", dialog)
        cmb_recip = QComboBox(dialog)
        for eid, name in active_employees:
            cmb_recip.addItem(name, eid)
        recip_layout.addWidget(recip_lbl)
        recip_layout.addWidget(cmb_recip)
        layout.addLayout(recip_layout)

        # Message text field
        msg_lbl = QLabel("Message:", dialog)
        layout.addWidget(msg_lbl)
        txt_msg = QTextEdit(dialog)
        txt_msg.setPlainText(f"Pre-/Post-enriched samples from Electrolysis Run #{self.run_id} are ready for SIAM.")
        txt_msg.setMaximumHeight(80)
        layout.addWidget(txt_msg)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_send = QPushButton("Send Alert", dialog)
        btn_skip = QPushButton("Skip", dialog)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_skip)
        btn_layout.addWidget(btn_send)
        layout.addLayout(btn_layout)

        btn_skip.clicked.connect(dialog.reject)
        
        def on_send():
            rid = cmb_recip.currentData()
            msg = txt_msg.toPlainText().strip()
            if not msg:
                show_message(dialog, "Message cannot be empty", "Warning", "warning")
                return
            
            try:
                with db_manager.get_connection() as conn:
                    conn.execute(text("""
                        INSERT INTO public.employeemessage (senderid, recipientid, message, isread, createdatestamp)
                        VALUES (:sid, :rid, :msg, FALSE, NOW())
                    """), {"sid": sender_id, "rid": rid, "msg": msg})
                    conn.commit()
                set_status(self.status_label, "SIAM Technician alert sent.", "success")
                dialog.accept()
            except Exception as ex:
                logging.error(f"Error inserting employee message: {ex}")
                show_message(dialog, f"Failed to send alert: {ex}", "Error", "error")

        btn_send.clicked.connect(on_send)
        dialog.exec_()


if __name__ == "__main__":
    from PyQt5.QtWidgets import QApplication
    app = QApplication(sys.argv)
    w = TrimsElectrolysisDetailsWindow(1000)
    w.show()
    sys.exit(app.exec_())
