"""
ngam_ingrowth_run_gui.py
========================
NGAM – 3H-3He Ingrowth: Single Run Detail / Edit Window

Mirrors trims_electrolysis_details_gui.py pattern.

Schema (ngam.ng3heingrowthdata columns):
    ingrowthid, runid, analysisid, repeat, iposition, status,
    fstaticleaktestbefore, fdegassinghours,
    dtimestart, dtimeend,
    fleaktestafter,
    fweightwaterbulbempty,  fweightwaterbulbbefore, fweightwaterbulbafter,
    nvcremarks, isignored, itritiumsuccessorid

Completion rules (from VBA SaveStatusChanges):
    • dtimestart  required
    • dtimeend    required
    • dtimestart  < dtimeend
    • fweightwaterbulbbefore and fweightwaterbulbafter not null
    • If (before − after) < 0: isignored = 1, remark appended
    • When ALL samples complete: RunStatus = 5, RunEndTime = now
    • Complete + not ignored samples → INSERT into public.sample_queue for next step
"""
from __future__ import annotations
import sys
import logging
import getpass
from datetime import datetime
from typing import Optional, Set

from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QLineEdit, QTextEdit, QDateTimeEdit, QComboBox,
    QPushButton, QTableView, QHeaderView, QStyledItemDelegate,
    QAbstractItemView, QAbstractItemDelegate, QMessageBox, QCheckBox,
    QStyle, QFrame, QWidget,
)
from PyQt5.QtCore import Qt, QEvent, QTimer, QDateTime, QRect, QSize
from PyQt5.QtGui import (
    QStandardItemModel, QStandardItem, QColor, QBrush, QDoubleValidator
)
from sample_label_gui import SampleLabelDialog, analysis_spec

from sqlalchemy import text
from shared_utils import check_employee_privilege, set_status, normalize_login_name, get_current_user_id
from gui_utils import show_message
from balance_serial import BalanceSerialReader, load_balance_equipment, list_available_ports


# ─────────────────────────────── column constants ────────────────────────────
class C:
    POS        = 0   # iposition
    AID        = 1   # analysisid
    SAMPLE     = 2   # prefix-sampleid name  (read-only)
    # ── group: Water Bulb Weights (g) ───────────────
    EMPTY      = 3   # fweightwaterbulbempty
    BEFORE     = 4   # fweightwaterbulbbefore
    AFTER      = 5   # fweightwaterbulbafter
    # ── group: Sample Water Mass (g) ────────────────
    WATER_BEF  = 6   # before − empty  (computed, read-only)
    WATER_AFT  = 7   # after  − empty  (computed, read-only)
    LOSS       = 8   # before − after  (computed, read-only)
    # ── group: Leak Test ────────────────────────────
    LEAK_BEF   = 9   # fstaticleaktestbefore
    LEAK_AFT   = 10  # fleaktestafter
    IGNORED    = 11  # isignored / Reject (checkbox)
    # ── standalone ──────────────────────────────────
    DEGAS_H    = 12  # fdegassinghours
    T_START    = 13  # dtimestart
    T_END      = 14  # dtimeend
    PERIOD     = 15  # ingrowth period (days, computed)
    STATUS     = 16  # data status
    REMARKS    = 17  # nvcremarks

    HEADERS = [
        "Pos", "AnalysisID", "Sample",
        "Empty (g)", "Before (g)", "After (g)",        # Water Bulb Weights
        "Before Degass", "After Degass", "Loss (g)",   # Sample Water Mass
        "Before", "After", "Reject",                   # Leak Test
        "Degas (h)", "Ingrowth Start", "Ingrowth End", "Period (d)",
        "Status", "Remarks",
    ]

    READ_ONLY = {POS, AID, SAMPLE, WATER_BEF, WATER_AFT, LOSS, PERIOD}

    # Genuine gravimetric (weight, g) columns, for the balance-link
    # immediate-commit path. Leak test/degas hours are not weights.
    BALANCE_TARGET_COLS = {EMPTY, BEFORE, AFTER}


# ─────────────────────────────── delegates ───────────────────────────────────

class _NumericDelegate(QStyledItemDelegate):
    """Inline float editor; Enter moves to next row in same column."""

    def __init__(self, parent=None, decimals: int = 3, active_cols: set = None):
        super().__init__(parent)
        self.decimals = decimals
        self.active_cols: set = active_cols if active_cols is not None else set()

    def createEditor(self, parent, option, index):
        if index.column() not in self.active_cols:
            return None
        ed = QLineEdit(parent)
        ed.setValidator(QDoubleValidator(-1e12, 1e12, self.decimals))
        ed.installEventFilter(self)
        return ed

    def setEditorData(self, editor, index):
        editor.setText(index.data(Qt.EditRole) or "")
        editor.selectAll()

    def setModelData(self, editor, model, index):
        txt = editor.text().strip()
        if txt:
            try:
                val = float(txt)
                model.setData(index, f"{val:.{self.decimals}f}", Qt.EditRole)
                model.setData(index, val, Qt.UserRole)
                return
            except ValueError:
                pass
        model.setData(index, "", Qt.EditRole)
        model.setData(index, None, Qt.UserRole)

    def eventFilter(self, editor, event):
        if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.commitData.emit(editor)
            view = self.parent()
            if isinstance(view, QTableView):
                cur = view.currentIndex()
                nrow = (cur.row() + 1) % view.model().rowCount()
                nidx = view.model().index(nrow, cur.column())
                view.setCurrentIndex(nidx)
                view.scrollTo(nidx)
            self.closeEditor.emit(editor, QAbstractItemDelegate.EditNextItem)
            return True
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.closeEditor.emit(editor, QAbstractItemDelegate.RevertModelCache)
            return True
        return super().eventFilter(editor, event)


class _DateTimeDelegate(QStyledItemDelegate):
    """
    Inline datetime editor (format yyyy-MM-dd HH:mm).
    Columns must be in active_cols to open.
    """
    FMT = "yyyy-MM-dd HH:mm"

    def __init__(self, parent=None, active_cols: set = None):
        super().__init__(parent)
        self.active_cols: set = active_cols if active_cols is not None else set()

    def createEditor(self, parent, option, index):
        if index.column() not in self.active_cols:
            return None
        ed = QDateTimeEdit(parent)
        ed.setDisplayFormat(self.FMT)
        ed.setCalendarPopup(True)
        ed.installEventFilter(self)
        return ed

    def setEditorData(self, editor, index):
        txt = index.data(Qt.EditRole) or ""
        try:
            dt = datetime.strptime(txt, "%Y-%m-%d %H:%M")
            editor.setDateTime(QDateTime(dt.year, dt.month, dt.day, dt.hour, dt.minute))
        except Exception:
            editor.setDateTime(QDateTime.currentDateTime())

    def setModelData(self, editor, model, index):
        dt = editor.dateTime().toPyDateTime()
        model.setData(index, dt.strftime("%Y-%m-%d %H:%M"), Qt.EditRole)
        model.setData(index, dt, Qt.UserRole)

    def eventFilter(self, editor, event):
        if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.commitData.emit(editor)
            self.closeEditor.emit(editor, QAbstractItemDelegate.EditNextItem)
            return True
        return super().eventFilter(editor, event)


class _LossDelegate(QStyledItemDelegate):
    """Colour-code the weight-loss column: green ≥ 0, red < 0."""
    _GREEN = QColor("#c8e6c9")
    _RED   = QColor("#ffcdd2")
    _GRAY  = QColor("#f5f5f5")

    def paint(self, painter, option, index):
        txt = index.data(Qt.DisplayRole) or ""
        try:
            val = float(txt)
            bg = self._GREEN if val >= 0 else self._RED
        except ValueError:
            bg = self._GRAY
        painter.save()
        painter.fillRect(option.rect, QBrush(bg))
        painter.restore()
        super().paint(painter, option, index)


# ─────────────────────────────── grouped header ──────────────────────────────

class _GroupHeaderView(QHeaderView):
    """
    Two-row horizontal header.
      Top  row — coloured, bold group-span labels.
      Bottom row — individual column names (normal header style).
    Columns not in any named group get the standard full-height header.
    """
    # (label_or_None, [col_indices]) — indices must match class C constants
    _GROUPS = [
        (None,                     list(range(0, 3))),   # Pos, AID, Sample
        ("Water Bulb Weights (g)", list(range(3, 6))),   # EMPTY, BEFORE, AFTER
        ("Sample Water Mass (g)",  list(range(6, 9))),   # WATER_BEF, WATER_AFT, LOSS
        ("Leak Test",              list(range(9, 12))),  # LEAK_BEF, LEAK_AFT, IGNORED
        (None,                     list(range(12, 18))), # DEGAS_H … REMARKS
    ]
    _ROW_H  = 20
    _GRP_BG = QColor("#C5CAE9")   # indigo-100
    _GRP_FG = QColor("#1A237E")   # indigo-900
    _GRP_BD = QColor("#9FA8DA")   # indigo-300

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        # col → (label, first_col, last_col)
        self._group_of: dict = {}
        for label, cols in self._GROUPS:
            if label:
                for c in cols:
                    self._group_of[c] = (label, cols[0], cols[-1])

    def sizeHint(self):
        s = super().sizeHint()
        return QSize(s.width(), s.height() + self._ROW_H)

    def paintSection(self, painter, rect, logical_index):
        if not rect.isValid():
            return
        ginfo = self._group_of.get(logical_index)
        if ginfo is None:
            # no group: normal full-height header
            super().paintSection(painter, rect, logical_index)
            return

        label, first_col, last_col = ginfo

        # ── bottom row: standard column-name header ───────────────────────────
        bot_rect = QRect(rect.x(), rect.y() + self._ROW_H,
                         rect.width(), rect.height() - self._ROW_H)
        super().paintSection(painter, bot_rect, logical_index)

        # ── top row: group span (every column in the group redraws it so the
        #            result is correct regardless of paint order) ──────────────
        x1 = self.sectionViewportPosition(first_col)
        x2 = self.sectionViewportPosition(last_col) + self.sectionSize(last_col)
        top_rect = QRect(x1, rect.y(), x2 - x1, self._ROW_H)

        painter.save()
        painter.setClipping(False)
        painter.fillRect(top_rect, self._GRP_BG)
        painter.setPen(self._GRP_BD)
        painter.drawRect(top_rect.adjusted(0, 0, -1, -1))
        f = painter.font()
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(self._GRP_FG)
        painter.drawText(top_rect, Qt.AlignCenter, label)
        painter.restore()


# ─────────────────────────────── main window ─────────────────────────────────
class NGAMIngrowthRunWindow(QDialog):
    """
    Master-detail dialog for a single 3He Ingrowth Run.
    Header = run metadata; Table = per-sample ingrowth measurements.
    """

    def __init__(self, run_id: int, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)
        self.run_id      = run_id
        self.is_editing  = False
        self.has_write   = False
        self._is_locked  = False
        self._updating   = False
        self._ingrowth_id_map: dict = {}   # row → ingrowthid (PK)
        self.status_desc: dict = {}        # code → description

        self.setWindowTitle(f"3He Ingrowth Run :: {run_id}")
        self.resize(1400, 780)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        # ── delegates ────────────────────────────────────────────────────────
        self._num_active: Set[int] = {C.EMPTY, C.BEFORE, C.AFTER,
                                       C.LEAK_BEF, C.DEGAS_H, C.LEAK_AFT}
        self._dt_active:  Set[int] = {C.T_START, C.T_END}

        self.num_delegate = _NumericDelegate(None, decimals=3, active_cols=self._num_active)
        self.dt_delegate  = _DateTimeDelegate(None, active_cols=self._dt_active)
        self.loss_delegate = _LossDelegate()

        # Balance link (RS-232/USB) -- bar only shown while editing. This
        # window already autosaves per-cell via itemChanged -> _save_row(),
        # so a balance-injected value just needs to land in the model cell;
        # the existing autosave does the immediate commit for free.
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
        self.lblBalanceReading.setStyleSheet("color:#444; font-family:monospace;")
        self.balance_reader = BalanceSerialReader(self)
        self.balance_reader.stableReading.connect(self._on_balance_reading)
        self.balance_reader.statusChanged.connect(self._on_balance_status)
        self.balance_reader.connectionChanged.connect(self._on_balance_connection_changed)

        self._build_ui()
        for eid, name in load_balance_equipment():
            self.cmbBalance.addItem(name, eid)

        try:
            self._check_privileges()
            self._load_lookups()
            self._load_header()
            self._load_detail()
            self._apply_read_only()
        except Exception as e:
            logging.error(f"NGAMIngrowthRunWindow init failed: {e}", exc_info=True)
            self.status_label.setText(f"Error: {e}")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)

        # top bar
        top = QHBoxLayout()
        self.btnEdit  = QPushButton("Edit"); self.btnEdit.setCheckable(True)
        self.btnPrintLabels = QPushButton("Print Labels")
        self.btnClose = QPushButton("Close")
        top.addStretch()
        top.addWidget(self.btnEdit)
        top.addWidget(self.btnPrintLabels)
        top.addWidget(self.btnClose)
        root.addLayout(top)

        # header form
        self._build_header_form()
        root.addWidget(self.hdr_group)

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
        root.addWidget(self.balance_bar_w)
        self.btnBalanceRefreshPorts.clicked.connect(self._refresh_balance_ports)
        self.btnBalanceConnect.clicked.connect(self._toggle_balance_connect)

        # detail table
        self.table = QTableView()
        self._apply_table_styles()
        self.model = QStandardItemModel()
        self.table.setModel(self.model)
        self.table.setHorizontalHeader(_GroupHeaderView(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)

        # assign delegates
        for _col in (C.LOSS, C.WATER_BEF, C.WATER_AFT):
            self.table.setItemDelegateForColumn(_col, self.loss_delegate)
        for c in self._num_active:
            self.table.setItemDelegateForColumn(c, self.num_delegate)
        for c in self._dt_active:
            self.table.setItemDelegateForColumn(c, self.dt_delegate)

        root.addWidget(self.table, 1)

        # status bar
        self.status_label = QLabel("Ready")
        root.addWidget(self.status_label)

        # ── connections ───────────────────────────────────────────────────────
        self.btnEdit.toggled.connect(self._toggle_edit)
        self.btnPrintLabels.clicked.connect(self._print_ingrowth_labels)
        self.btnClose.clicked.connect(self.accept)
        self.model.itemChanged.connect(self._on_item_changed)
        self.chkFinished.toggled.connect(self._on_finished_toggled)
        self.dtEnd.dateTimeChanged.connect(self._on_end_date_changed)

    def _print_ingrowth_labels(self):
        """Fetch samples for this ingrowth run and open the QR label printer."""
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT d.iposition,
                           a.prefix, a.sampleid, s.sname,
                           d.analysisid
                    FROM   ngam.ng3heingrowthdata d
                    JOIN   public.analysis a ON a.analysisid = d.analysisid
                    JOIN   public.sample   s ON s.sampleid   = a.sampleid
                                             AND s.prefix     = a.prefix
                    WHERE  d.runid = :r
                    ORDER  BY d.iposition
                """), {"r": self.run_id}).fetchall()
        except Exception as exc:
            logging.error("Ingrowth label fetch: %s", exc)
            show_message(self, "Error", str(exc))
            return
        if not rows:
            show_message(self, "No Data", "No samples found for this run.")
            return
        specs = [
            analysis_spec(
                prefix=r.prefix or "",
                sample_id=str(r.sampleid),
                analysis_id=str(r.analysisid),
                container_num=f"Pos {r.iposition}",
                job_type="Ingrowth",
            )
            for r in rows
        ]
        SampleLabelDialog.show_batch(specs, parent=self).exec_()

    def _build_header_form(self):
        self.hdr_group = QGroupBox("Run Metadata")
        self.hdr_group.setStyleSheet("""
            QGroupBox { background:#FAFAFC; border:1px solid #D9D9E3;
                        border-radius:8px; margin-top:8px; font-weight:600; padding-top:10px; }
            QGroupBox::title { subcontrol-origin:margin; left:10px; padding:0 6px; color:#2D2D33; }
            QLineEdit, QComboBox, QDateTimeEdit, QTextEdit { background:#fff; border:1px solid #D0D0DD;
                border-radius:5px; padding:3px 5px; }
            QDateTimeEdit::drop-down { background: #C5CAE9; border: none; width: 22px; border-radius: 0 6px 6px 0; }
            QDateTimeEdit::drop-down:hover { background: #7986CB; }
            QDateTimeEdit::drop-down:pressed { background: #3949AB; }
            QDateTimeEdit::down-arrow { image: none; width: 0; height: 0; border-left: 5px solid transparent; border-right: 5px solid transparent; border-top: 6px solid #3949AB; }
        """)
        grid = QGridLayout(self.hdr_group)
        grid.setHorizontalSpacing(16); grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1); grid.setColumnStretch(3, 1); grid.setColumnStretch(5, 1)

        def lbl(t):
            l = QLabel(t); l.setAlignment(Qt.AlignRight | Qt.AlignVCenter); return l

        self.txtRunID = QLineEdit(); self.txtRunID.setReadOnly(True)
        self.txtRunID.setFixedWidth(80)
        self.txtRunID.setStyleSheet("background:#F2F3F7; font-weight:600;")

        self.cmbTech = QComboBox()

        self.dtStart = QDateTimeEdit(); self.dtStart.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dtStart.setCalendarPopup(True); self.dtStart.setFixedWidth(160)

        self.dtEnd = QDateTimeEdit(); self.dtEnd.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dtEnd.setCalendarPopup(True); self.dtEnd.setFixedWidth(160)
        self._lbl_ongoing = QLabel("— ongoing —")
        self._lbl_ongoing.setFixedWidth(160)
        self._lbl_ongoing.setStyleSheet(
            "color:#9E9E9E; font-style:italic; padding:3px 6px;"
            "border:1px solid #D0D0DD; border-radius:5px; background:#F9F9FB;")
        self.chkFinished = QCheckBox("Finished?")
        h_end = QHBoxLayout()
        h_end.addWidget(self._lbl_ongoing)
        h_end.addWidget(self.dtEnd)
        h_end.addWidget(self.chkFinished)

        self.cmbEquipment = QComboBox()
        self.txtRemarks   = QTextEdit(); self.txtRemarks.setMaximumHeight(50)

        grid.addWidget(lbl("Run ID:"), 0, 0);  grid.addWidget(self.txtRunID, 0, 1)
        grid.addWidget(lbl("Technician:"), 1, 0); grid.addWidget(self.cmbTech, 1, 1)
        grid.addWidget(lbl("Start:"), 0, 2);   grid.addWidget(self.dtStart, 0, 3)
        grid.addWidget(lbl("End:"),   1, 2);   grid.addLayout(h_end,   1, 3)
        grid.addWidget(lbl("Equipment:"), 0, 4); grid.addWidget(self.cmbEquipment, 0, 5)
        grid.addWidget(lbl("Remarks:"), 2, 0, Qt.AlignTop); grid.addWidget(self.txtRemarks, 2, 1, 1, 5)

    def _apply_table_styles(self):
        self.table.setShowGrid(True)
        self.table.setStyleSheet("""
            QTableView { border:1px solid #d0d0d0; background:white;
                         gridline-color:#cccccc; selection-background-color:#DDEEFF; }
            QTableView::item { padding:4px; }
            QHeaderView::section { background:#f0f0f0; font-weight:bold;
                                   border:1px solid #d0d0d0; padding:4px; }
        """)

    # ── privileges ────────────────────────────────────────────────────────────

    def _check_privileges(self):
        try:
            user = normalize_login_name(get_current_user_id())
            self.has_write = check_employee_privilege(user, "ngamaccess")
        except Exception:
            self.has_write = False

    # ── lookups ───────────────────────────────────────────────────────────────

    def _load_lookups(self):
        with db_manager.get_connection() as conn:
            self.cmbTech.addItem("", None)
            for r in conn.execute(text(
                "SELECT EmployeeID, LastName, FirstMiddleName FROM Employee ORDER BY LastName"
            )):
                self.cmbTech.addItem(f"{r[1]}, {r[2]}", r[0])

            self.cmbEquipment.addItem("", None)
            for r in conn.execute(text(
                "SELECT EquipmentID, EquipmentName FROM Equipment ORDER BY EquipmentName"
            )):
                self.cmbEquipment.addItem(r[1], r[0])

            # status lookup
            try:
                for r in conn.execute(text(
                    "SELECT Status, Description FROM StatusLookup "
                    "WHERE Status IN (4,5,-9) ORDER BY Status"
                )):
                    self.status_desc[int(r[0])] = str(r[1])
            except Exception:
                self.status_desc = {4: "In Progress", 5: "Complete", -9: "Cancelled"}

    # ── header ────────────────────────────────────────────────────────────────

    def _load_header(self):
        sql = text("""
            SELECT RunID, RunStartTime, RunEndTime, TechnicianID,
                   EquipmentID, RunStatus, Remarks, IsLocked
            FROM NGAM.NG3HeIngrowthRun
            WHERE RunID = :rid
        """)
        with db_manager.get_connection() as conn:
            row = conn.execute(sql, {"rid": self.run_id}).fetchone()
        if not row:
            set_status(self.status_label, "Run not found.", "error")
            return

        self.txtRunID.setText(str(row[0]))
        if row[1]:
            self.dtStart.setDateTime(
                QDateTime(row[1].year, row[1].month, row[1].day, row[1].hour, row[1].minute))
        is_finished = bool(row[2])
        if is_finished:
            self.dtEnd.setDateTime(
                QDateTime(row[2].year, row[2].month, row[2].day, row[2].hour, row[2].minute))
        self.chkFinished.setChecked(is_finished)
        self._on_finished_toggled(is_finished)   # force label/picker visibility

        self._set_combo(self.cmbTech,      row[3])
        self._set_combo(self.cmbEquipment, row[4])
        self.txtRemarks.setPlainText(row[6] or "")
        self._is_locked = bool(row[7]) if row[7] is not None else False

    def _set_combo(self, cmb, val):
        if val is None:
            cmb.setCurrentIndex(0); return
        idx = cmb.findData(val)
        cmb.setCurrentIndex(idx if idx >= 0 else 0)

    # ── detail table ──────────────────────────────────────────────────────────

    def _load_detail(self):
        sql = text("""
            SELECT
                d.IngrowthID,
                d.IPosition,
                d.AnalysisID,
                a.SampleID,
                a.Prefix,
                s.sName,
                d.FWeightWaterBulbEmpty,
                d.FWeightWaterBulbBefore,
                d.FWeightWaterBulbAfter,
                d.FStaticLeakTestBefore,
                d.FDegassingHours,
                d.FLeakTestAfter,
                d.DTimeStart,
                d.DTimeEnd,
                d.IsIgnored,
                d.Status,
                d.NvcRemarks
            FROM NGAM.NG3HeIngrowthData d
            JOIN Analysis a ON d.AnalysisID = a.AnalysisID
            JOIN Sample   s ON a.SampleID   = s.SampleID AND a.Prefix = s.Prefix
            WHERE d.RunID = :rid
            ORDER BY d.IPosition, d.AnalysisID
        """)
        self._updating = True
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(sql, {"rid": self.run_id}).fetchall()

            self.model.clear()
            self.model.setHorizontalHeaderLabels(C.HEADERS)
            self._ingrowth_id_map.clear()
            brush_ro = QBrush(QColor("#f5f5f5"))

            for r in rows:
                (ingrowth_id, pos, aid, sid, prefix, name,
                 w_empty, w_before, w_after,
                 leak_bef, degas_h, leak_aft,
                 t_start, t_end,
                 is_ignored, status_code, remarks) = r

                water_bef = self._calc_water_mass(w_before, w_empty)
                water_aft = self._calc_water_mass(w_after,  w_empty)
                loss   = self._calc_loss(w_before, w_after)
                period = self._calc_period(t_start, t_end)
                status_code = int(status_code) if status_code else 4
                status_desc = self.status_desc.get(status_code, str(status_code))

                items = [
                    QStandardItem(str(pos)),                          # C.POS       0
                    QStandardItem(str(aid)),                          # C.AID       1
                    QStandardItem(f"{prefix}-{sid}  {name or ''}"),   # C.SAMPLE    2
                    # Water Bulb Weights (g)
                    QStandardItem(self._fmt(w_empty)),                # C.EMPTY     3
                    QStandardItem(self._fmt(w_before)),               # C.BEFORE    4
                    QStandardItem(self._fmt(w_after)),                # C.AFTER     5
                    # Sample Water Mass (g)
                    QStandardItem(self._fmt(water_bef)),              # C.WATER_BEF 6
                    QStandardItem(self._fmt(water_aft)),              # C.WATER_AFT 7
                    QStandardItem(self._fmt(loss)),                   # C.LOSS      8
                    # Leak Test
                    QStandardItem(self._fmt(leak_bef)),               # C.LEAK_BEF  9
                    QStandardItem(self._fmt(leak_aft)),               # C.LEAK_AFT  10
                    QStandardItem(""),                                # C.IGNORED   11 (checkbox)
                    # Standalone
                    QStandardItem(self._fmt(degas_h)),                # C.DEGAS_H   12
                    QStandardItem(self._dt_str(t_start)),             # C.T_START   13
                    QStandardItem(self._dt_str(t_end)),               # C.T_END     14
                    QStandardItem(self._fmt_days(period)),            # C.PERIOD    15
                    QStandardItem(status_desc),                       # C.STATUS    16
                    QStandardItem(remarks or ""),                     # C.REMARKS   17
                ]

                # mark read-only cells
                for c in C.READ_ONLY:
                    items[c].setEditable(False)
                    items[c].setBackground(brush_ro)
                    items[c].setFlags(items[c].flags() & ~Qt.ItemIsEditable)

                # checkbox for Ignored
                chk = items[C.IGNORED]
                chk.setCheckable(True)
                chk.setCheckState(Qt.Checked if is_ignored else Qt.Unchecked)
                chk.setFlags(
                    (chk.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    & ~Qt.ItemIsEditable
                )
                # store data roles
                items[C.AID].setData(aid, Qt.UserRole)
                items[C.STATUS].setData(status_code, Qt.UserRole)

                row_idx = self.model.rowCount()
                self.model.appendRow(items)
                self._ingrowth_id_map[row_idx] = ingrowth_id

            self.table.resizeColumnsToContents()
            self.table.horizontalHeader().setSectionResizeMode(C.REMARKS, QHeaderView.Stretch)
            self.table.setWordWrap(True)
            self.table.resizeRowsToContents()
        finally:
            self._updating = False
        self._update_cell_flags()

    # ── edit mode ─────────────────────────────────────────────────────────────

    def _toggle_edit(self, checked: bool):
        if not self.has_write or self._is_locked:
            self.btnEdit.setChecked(False)
            if self._is_locked:
                show_message(self, "Run Locked", "This run has been signed off and cannot be edited.")
            else:
                show_message(self, "Access Denied", "You do not have write access to NGAM.", QMessageBox.Warning)
            return
        self.is_editing = checked
        if checked:
            self.btnEdit.setText("Stop Edit")
            self.table.setEditTriggers(QAbstractItemView.AllEditTriggers)
            set_status(self.status_label, "Edit Mode – modify values then Stop Edit to save.", "processing")
            self._refresh_balance_ports()
            self.balance_bar_w.setVisible(True)
        else:
            self._save_header()
            self.btnEdit.setText("Edit")
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            set_status(self.status_label, "Read Only", "neutral")
            self._apply_read_only()
            self.balance_reader.disconnect_port()
            self.balance_bar_w.setVisible(False)
        self._update_cell_flags()

    # ── balance link (RS-232/USB) ---------------------------------------------

    def _refresh_balance_ports(self):
        current = self.cmbPort.currentText()
        self.cmbPort.clear()
        self.cmbPort.addItems(list_available_ports())
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
        and advance to the next row, same column -- mirrors web's
        injectBalanceValue()/advanceFocus(). Writing via setText() alone
        triggers the existing itemChanged -> _save_row() autosave, so no
        separate commit call is needed here."""
        if not self.is_editing:
            return
        idx = self.table.currentIndex()
        if not idx.isValid() or idx.column() not in C.BALANCE_TARGET_COLS:
            return
        row, col = idx.row(), idx.column()
        if row >= self.model.rowCount():
            return

        self.model.item(row, col).setText(f"{value:.3f}")

        next_row = (row + 1) % self.model.rowCount()
        next_idx = self.model.index(next_row, col)
        self.table.setCurrentIndex(next_idx)
        self.table.scrollTo(next_idx)

    def closeEvent(self, event):
        self.balance_reader.disconnect_port()
        super().closeEvent(event)

    def _apply_read_only(self):
        for w in [self.dtStart, self.txtRemarks]:
            w.setReadOnly(True)
        self.dtEnd.setReadOnly(True)
        self.chkFinished.setEnabled(False)
        self.cmbTech.setEnabled(False)
        self.cmbEquipment.setEnabled(False)
        self.btnEdit.setEnabled(self.has_write and not self._is_locked)
        # read mode: show "ongoing" label or the actual date
        is_finished = self.chkFinished.isChecked()
        self._lbl_ongoing.setVisible(not is_finished)
        self.dtEnd.setVisible(is_finished)

    def _apply_edit_state(self):
        for w in [self.dtStart, self.txtRemarks]:
            w.setReadOnly(False)
        self.dtEnd.setReadOnly(False)
        self.cmbTech.setEnabled(True)
        self.cmbEquipment.setEnabled(True)
        # edit mode: always show the date picker; hide the "ongoing" label
        self._lbl_ongoing.setVisible(False)
        self.dtEnd.setVisible(True)
        # chkFinished is only enabled after the user picks a valid date
        dt_py = self.dtEnd.dateTime().toPyDateTime()
        self.chkFinished.setEnabled(dt_py >= datetime(2000, 1, 2))

    def _on_end_date_changed(self, qdt):
        """Called whenever the dtEnd picker value changes."""
        if not self.is_editing:
            return
        dt_py = qdt.toPyDateTime()
        date_valid = dt_py >= datetime(2000, 1, 2)
        self.chkFinished.setEnabled(date_valid)
        if not date_valid:
            # clear the checkbox without triggering propagation
            self.chkFinished.blockSignals(True)
            self.chkFinished.setChecked(False)
            self.chkFinished.blockSignals(False)
        elif self.chkFinished.isChecked():
            # date changed while already marked finished → re-propagate
            self._propagate_end_time_to_samples()

    def _on_finished_toggled(self, checked: bool):
        if not self.is_editing:
            # read mode: toggle label vs. date display
            self._lbl_ongoing.setVisible(not checked)
            self.dtEnd.setVisible(checked)
        else:
            # edit mode: dtEnd always visible; propagate end time when marking finished
            if checked:
                self._propagate_end_time_to_samples()

    def _propagate_end_time_to_samples(self) -> None:
        """Write the run end-time to every sample's Ingrowth End cell and persist."""
        end_dt  = self.dtEnd.dateTime().toPyDateTime()
        end_str = end_dt.strftime("%Y-%m-%d %H:%M")
        self._updating = True
        try:
            for r in range(self.model.rowCount()):
                it = self.model.item(r, C.T_END)
                if it is None:
                    continue
                it.setText(end_str)
                it.setData(end_dt, Qt.UserRole)
                # recalc period
                p_it = self.model.item(r, C.PERIOD)
                if p_it is not None:
                    t_start = self._parse_dt(r, C.T_START)
                    p_it.setText(self._fmt_days(self._calc_period(t_start, end_dt)))
        finally:
            self._updating = False
        for r in range(self.model.rowCount()):
            self._save_row(r)

    def _update_cell_flags(self):
        self.model.blockSignals(True)
        try:
            for r in range(self.model.rowCount()):
                for c in range(self.model.columnCount()):
                    it = self.model.item(r, c)
                    if it is None:
                        continue
                    if c in C.READ_ONLY:
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    elif c == C.IGNORED:
                        if self.is_editing:
                            it.setFlags(
                                (it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                                & ~Qt.ItemIsEditable
                            )
                        else:
                            it.setFlags(it.flags() & ~Qt.ItemIsUserCheckable & ~Qt.ItemIsEditable)
                    elif c == C.STATUS:
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)   # status set by save logic
                    elif self.is_editing:
                        it.setFlags(it.flags() | Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    else:
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
        finally:
            self.model.blockSignals(False)
        self.table.viewport().update()
        if self.is_editing:
            self._apply_edit_state()

    # ── item changed → recalc + save row ─────────────────────────────────────

    def _on_item_changed(self, item):
        if self._updating or not self.is_editing:
            return
        self._updating = True
        try:
            r, c = item.row(), item.column()

            # recalculate Water Masses and Loss
            if c in {C.EMPTY, C.BEFORE, C.AFTER}:
                w_empty  = self._parse_float(r, C.EMPTY)
                w_before = self._parse_float(r, C.BEFORE)
                w_after  = self._parse_float(r, C.AFTER)
                self.model.item(r, C.WATER_BEF).setText(self._fmt(self._calc_water_mass(w_before, w_empty)))
                self.model.item(r, C.WATER_AFT).setText(self._fmt(self._calc_water_mass(w_after,  w_empty)))
                self.model.item(r, C.LOSS).setText(self._fmt(self._calc_loss(w_before, w_after)))

            # recalculate Period
            if c in {C.T_START, C.T_END}:
                t_start = self._parse_dt(r, C.T_START)
                t_end   = self._parse_dt(r, C.T_END)
                period  = self._calc_period(t_start, t_end)
                self.model.item(r, C.PERIOD).setText(self._fmt_days(period))

            # auto-check Ignored if weight loss is negative
            if c in {C.BEFORE, C.AFTER}:
                w_before = self._parse_float(r, C.BEFORE)
                w_after  = self._parse_float(r, C.AFTER)
                if w_before is not None and w_after is not None:
                    if (w_before - w_after) < 0:
                        chk = self.model.item(r, C.IGNORED)
                        if chk.checkState() != Qt.Checked:
                            chk.setCheckState(Qt.Checked)
                            rem = self.model.item(r, C.REMARKS)
                            note = "negative loss of weight after degassing"
                            if note not in (rem.text() or ""):
                                rem.setText((rem.text() + "; " + note).lstrip("; "))

            # persist row to DB on any change
            self._save_row(r)

        finally:
            self._updating = False

    # ── header save ───────────────────────────────────────────────────────────

    def _save_header(self):
        if not self.has_write:
            return
        end_time   = self.dtEnd.dateTime().toPyDateTime() if self.chkFinished.isChecked() else None
        run_status = 5 if end_time else 4
        now        = datetime.now()
        user_stamp = normalize_login_name(get_current_user_id())
        remarks    = self.txtRemarks.toPlainText().strip()[:255]

        try:
            with db_manager.get_connection() as conn:
                conn.execute(text(f"""
                    UPDATE NGAM.NG3HeIngrowthRun
                    SET RunStartTime  = :st,
                        RunEndTime    = :et,
                        TechnicianID  = :tech,
                        EquipmentID   = :equip,
                        RunStatus     = :rs,
                        Remarks       = :rem,
                        ModifDateStamp = :now,
                        ModifUserStamp = :usr
                    WHERE RunID = :rid
                """), {
                    "st":    self.dtStart.dateTime().toPyDateTime(),
                    "et":    end_time,
                    "tech":  self.cmbTech.currentData(),
                    "equip": self.cmbEquipment.currentData(),
                    "rs":    run_status,
                    "rem":   remarks,
                    "now":   now,
                    "usr":   user_stamp,
                    "rid":   int(self.run_id),
                })
                conn.commit()
            set_status(self.status_label, "Metadata saved.", "success")

            # when run is being finished: validate all samples + update status
            if end_time:
                self._save_status_changes(run_status)

        except Exception as e:
            logging.error(f"Header save failed: {e}")
            set_status(self.status_label, f"Metadata Error: {e}", "error")

    # ── row save ─────────────────────────────────────────────────────────────

    def _save_row(self, r: int):
        ingrowth_id = self._ingrowth_id_map.get(r)
        if ingrowth_id is None:
            return

        def g(c):
            return self._parse_float(r, c)

        def dt(c):
            return self._parse_dt(r, c)

        is_ignored = self.model.item(r, C.IGNORED).checkState() == Qt.Checked
        remarks    = self.model.item(r, C.REMARKS).text()
        now        = datetime.now()
        user_stamp = normalize_login_name(get_current_user_id())

        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE NGAM.NG3HeIngrowthData
                    SET FWeightWaterBulbEmpty  = :empty,
                        FWeightWaterBulbBefore = :before,
                        FWeightWaterBulbAfter  = :after,
                        FStaticLeakTestBefore  = :leak_b,
                        FDegassingHours        = :degas,
                        FLeakTestAfter         = :leak_a,
                        DTimeStart             = :tstart,
                        DTimeEnd               = :tend,
                        IsIgnored              = :ign,
                        NvcRemarks             = :rem
                    WHERE IngrowthID = :iid
                """), {
                    "empty":   g(C.EMPTY),
                    "before":  g(C.BEFORE),
                    "after":   g(C.AFTER),
                    "leak_b":  g(C.LEAK_BEF),
                    "degas":   g(C.DEGAS_H),
                    "leak_a":  g(C.LEAK_AFT),
                    "tstart":  dt(C.T_START),
                    "tend":    dt(C.T_END),
                    "ign":     1 if is_ignored else 0,
                    "rem":     remarks,
                    "iid":     ingrowth_id,
                })
                conn.commit()
            set_status(self.status_label, f"Row {r+1} saved.", "success")
        except Exception as e:
            logging.error(f"Row {r} save failed: {e}")
            set_status(self.status_label, f"Save Error row {r+1}: {e}", "error")

    # ── status propagation + sample_queue insert ──────────────────────────────

    def _save_status_changes(self, run_status: int):
        """
        Validate every sample row, update NG3HeIngrowthData.Status,
        update NG3HeIngrowthRun.RunStatus, and if all complete queue for next step.
        """
        now        = datetime.now()
        user_stamp = normalize_login_name(get_current_user_id())
        errors: list = []
        complete_count = 0
        total = self.model.rowCount()

        for r in range(total):
            ingrowth_id = self._ingrowth_id_map.get(r)
            if ingrowth_id is None:
                continue

            aid     = self._parse_int(r, C.AID)
            t_start = self._parse_dt(r, C.T_START)
            t_end   = self._parse_dt(r, C.T_END)
            w_bef   = self._parse_float(r, C.BEFORE)
            w_aft   = self._parse_float(r, C.AFTER)
            ignored = self.model.item(r, C.IGNORED).checkState() == Qt.Checked

            # validation rules (from VBA SaveStatusChanges)
            if t_start is None:
                errors.append(f"Row {r+1}: Ingrowth Start time required.")
                new_status = 4
            elif t_end is None:
                new_status = 4
            elif t_start >= t_end:
                errors.append(f"Row {r+1}: Start time must be before End time.")
                new_status = 4
            elif w_bef is None or w_aft is None:
                new_status = 4
            else:
                new_status = 5
                complete_count += 1

            # update status in model
            desc = self.status_desc.get(new_status, str(new_status))
            self.model.item(r, C.STATUS).setText(desc)
            self.model.item(r, C.STATUS).setData(new_status, Qt.UserRole)

            try:
                with db_manager.get_connection() as conn:
                    conn.execute(text("""
                        UPDATE NGAM.NG3HeIngrowthData
                        SET Status = :st
                        WHERE IngrowthID = :iid
                    """), {"st": new_status, "iid": ingrowth_id})
                    conn.commit()
            except Exception as e:
                errors.append(f"Row {r+1} status update error: {e}")

        if errors:
            QMessageBox.warning(
                self, "Validation Issues",
                "Some samples are not complete:\n" + "\n".join(errors)
            )

        # update run status
        all_complete = (complete_count == total and total > 0)
        final_rs  = 5 if all_complete else 4
        final_end = now if all_complete else None

        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE NGAM.NG3HeIngrowthRun
                    SET RunStatus = :rs, RunEndTime = :et,
                        ModifDateStamp = :now, ModifUserStamp = :usr
                    WHERE RunID = :rid
                """), {"rs": final_rs, "et": final_end,
                       "now": now, "usr": user_stamp, "rid": int(self.run_id)})
                conn.commit()
        except Exception as e:
            logging.error(f"Run status update failed: {e}")

        # if all complete → queue for next stage (3He sequence)
        if all_complete:
            self._insert_sample_tba(now, user_stamp)
            set_status(self.status_label,
                       "Run complete. All samples queued for next step.", "success")
        else:
            set_status(self.status_label,
                       f"{complete_count}/{total} samples complete.", "processing")

    def _insert_sample_tba(self, now: datetime, user_stamp: str):
        """
        Advance completed, non-ignored samples to the next workflow step queue
        (3He sequence) using the unified stored procedure public.sp_stage_forward.
        Also locks the run.
        """
        try:
            with db_manager.get_connection() as conn:
                # 1. Lock the run
                conn.execute(text("""
                    UPDATE NGAM.NG3HeIngrowthRun
                    SET IsLocked = TRUE
                    WHERE RunID = :rid
                """), {"rid": int(self.run_id)})

                # 2. Collect eligible analysisids (completed, status=5, not ignored)
                # and not already measured at the downstream MS step (not in ng3hesequenceloadlist)
                eligible_aids = [
                    r[0] for r in conn.execute(text("""
                        SELECT d.analysisid
                        FROM ngam.ng3heingrowthdata d
                        WHERE d.runid = :rid
                          AND d.status = 5
                          AND (d.isignored IS NULL OR d.isignored = 0)
                          AND NOT EXISTS (
                              SELECT 1 FROM ngam.ng3hesequenceloadlist sl WHERE sl.analysisid = d.analysisid
                          )
                    """), {"rid": int(self.run_id)}).fetchall()
                ]

                # 3. Resolve this run's workflowjobid
                row_wjid = conn.execute(text("""
                    SELECT workflowjobid FROM ngam.ng3heingrowthrun WHERE runid = :rid
                """), {"rid": int(self.run_id)}).fetchone()
                
                ing_wjid = row_wjid[0] if row_wjid else None

                # 4. Advance queue
                staged = 0
                if eligible_aids and ing_wjid:
                    rows = conn.execute(text("""
                        SELECT analysisid, staged, skip_reason
                        FROM public.sp_stage_forward(:wjid, :aids, 'desktop')
                    """), {"wjid": ing_wjid, "aids": eligible_aids}).fetchall()
                    staged = sum(1 for r in rows if r[1])

                conn.commit()
                logging.info(f"NGAM ingrowth run {self.run_id} finalized and locked. Staged {staged} samples.")
                self._is_locked = True
                
        except Exception as e:
            logging.error(f"Ingrowth queue staging failed: {e}", exc_info=True)
            QMessageBox.warning(self, "Queue Error", f"Could not queue samples for next step:\n{e}")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _parse_float(self, row: int, col: int) -> Optional[float]:
        it = self.model.item(row, col)
        if it is None:
            return None
        # prefer UserRole (set by delegate)
        ur = it.data(Qt.UserRole)
        if ur is not None:
            try:
                return float(ur)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        try:
            return float(it.text())
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return None

    def _parse_int(self, row: int, col: int) -> Optional[int]:
        v = self._parse_float(row, col)
        return int(v) if v is not None else None

    def _parse_dt(self, row: int, col: int) -> Optional[datetime]:
        it = self.model.item(row, col)
        if it is None:
            return None
        # prefer UserRole (set by delegate)
        ur = it.data(Qt.UserRole)
        if isinstance(ur, datetime):
            return ur
        try:
            return datetime.strptime(it.text(), "%Y-%m-%d %H:%M")
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return None

    def _calc_water_mass(self, w_bulb, w_empty) -> Optional[float]:
        if w_bulb is not None and w_empty is not None:
            return w_bulb - w_empty
        return None

    def _calc_loss(self, w_before, w_after) -> Optional[float]:
        if w_before is not None and w_after is not None:
            return w_before - w_after
        return None

    def _calc_period(self, t_start, t_end) -> Optional[float]:
        if t_start and t_end and t_end > t_start:
            return (t_end - t_start).total_seconds() / 86400.0
        return None

    def _fmt(self, val) -> str:
        if val is None:
            return ""
        try:
            return f"{float(val):.3f}"
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return str(val)

    def _fmt_days(self, val) -> str:
        if val is None:
            return ""
        try:
            return f"{float(val):.2f}"
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return ""

    def _dt_str(self, val) -> str:
        if val is None:
            return ""
        try:
            return val.strftime("%Y-%m-%d %H:%M")
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return str(val)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    w = NGAMIngrowthRunWindow(1)
    w.show()
    sys.exit(app.exec_())
