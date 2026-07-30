"""
ngam_extraction_run_gui.py
===========================
NGAM – Noble Gas Extraction: Single Run Detail / Edit Window

Matches the MS Access frmNobleGasExtractionRun form.

Schema (ngam.ngextractionrun):
    runid, procedureid, equipmentid, workflowid, technicianid,
    islocked, runstatus (4=pending, 5=complete),
    runstarttime, runendtime, remarks,
    createdatestamp, createuserstamp, modifdatestamp, modifuserstamp

Schema (ngam.ngextractiondata):
    runid, iposition, analysisid,
    fstaticleaktestbefore, dtimestart, dtimeend, fleaktestafter,
    fweighttubebefore, fweighttubeafter,
    fweightgasbulbbefore, fweightgasbulbafter,
    fweightwaterbulbbefore, fweightwaterbulbafter,
    status, nvcremarks, isignored, igassuccessorid
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
    QStyle, QFrame, QSizePolicy, QToolButton, QWidget,
)
from PyQt5.QtCore import Qt, QEvent, QDateTime, QRect, QSize
from PyQt5.QtGui import (
    QStandardItemModel, QStandardItem, QColor, QBrush, QDoubleValidator, QPainter,
)
from sample_label_gui import SampleLabelDialog, analysis_spec

from db_core import db_manager
from sqlalchemy import text
from shared_utils import (
    check_employee_privilege, normalize_login_name, get_current_user_id,
    get_global_value,
)
from gui_utils import show_message
from help_browser import make_help_button
from balance_serial import BalanceSerialReader, load_balance_equipment, list_available_ports


# ─────────────────────────────── column constants ────────────────────────────
class C:
    POS         = 0    # iposition (r/o)
    AID         = 1    # analysisid (r/o)
    LABID       = 2    # prefix-sampleid  (r/o)
    SNAME       = 3    # sample name (r/o)
    T_START     = 4    # dtimestart
    T_END       = 5    # dtimeend
    # Tube weights
    TUBE_BEF    = 6    # fweighttubebefore
    TUBE_AFT    = 7    # fweighttubeafter
    TUBE_NET    = 8    # derived: before − after  (r/o)
    # Gas bulb weights
    GAS_BEF     = 9    # fweightgasbulbbefore
    GAS_AFT     = 10   # fweightgasbulbafter
    GAS_NET     = 11   # derived: after − before  (r/o, green≥0 / red<0)
    # Water bulb weights
    WATER_BEF   = 12   # fweightwaterbulbbefore
    WATER_AFT   = 13   # fweightwaterbulbafter
    WATER_NET   = 14   # derived: after − before  (r/o)
    # Leak test
    LEAK_BEF    = 15   # fstaticleaktestbefore
    LEAK_AFT    = 16   # fleaktestafter
    # Meta
    STATUS      = 17   # status (r/o, computed)
    IGNORED     = 18   # isignored
    REMARKS     = 19   # nvcremarks
    # Extraction correction (migration 019/023)
    TEMP_C      = 20   # temperature_c         — field sampling temperature (°C)
    LAB_PRESS   = 21   # lab_pressure_torr — measured barometric pressure (Torr) for EQW
    SALINITY    = 22   # salinity_ppt          — salinity in g/kg
    ALTITUDE    = 23   # altitude_m            — site altitude (m)
    EFFICIENCY  = 24   # extraction_efficiency — η fraction (0–1)
    # Diffusion sampler volume (migration 021)
    SAMPLE_VOL  = 25   # sample_volume_ml — water volume for diffusion samplers (type 3)

    HEADERS = [
        "Inlet", "AnalysisID", "Lab ID", "Sample",
        "Time Start", "Time End",
        "Before (g)", "After (g)", "Net (g)",        # Tube
        "Before (g)", "After (g)", "Gas Net (g)",    # Gas Bulb
        "Before (g)", "After (g)", "Water Mass (g)", # Water Bulb (net = effective water mass)
        "Before", "After",                           # Leak Test
        "Status", "Ignored?", "Remarks",
        "Temp (°C)", "Lab P. (torr)", "Salinity (‰)", "Altitude (m)", "Extr. Eff.",
        "Vol (mL)",
    ]

    READ_ONLY = {POS, AID, LABID, SNAME, TUBE_NET, GAS_NET, WATER_NET, STATUS}

    # Numeric (double) columns that are directly stored
    NUMERIC = {TUBE_BEF, TUBE_AFT, GAS_BEF, GAS_AFT, WATER_BEF, WATER_AFT,
               LEAK_BEF, LEAK_AFT, TEMP_C, SALINITY, ALTITUDE, EFFICIENCY,
               SAMPLE_VOL, LAB_PRESS}
    # Datetime columns
    DATETIME = {T_START, T_END}

    # Genuine gravimetric (weight, g) columns -> ngam.ngextractiondata column
    # name, for the balance-link immediate-commit path. Leak test/temp/
    # salinity/altitude/efficiency/volume are not weights, excluded.
    BALANCE_TARGET_COLS = {
        TUBE_BEF: "fweighttubebefore", TUBE_AFT: "fweighttubeafter",
        GAS_BEF: "fweightgasbulbbefore", GAS_AFT: "fweightgasbulbafter",
        WATER_BEF: "fweightwaterbulbbefore", WATER_AFT: "fweightwaterbulbafter",
    }


# ─────────────────────────────── palette ─────────────────────────────────────
_HDR_BG    = "#1F3A5F"
_PANEL_BG  = "#EAF0F6"
_GRP_BG    = QColor("#C5CAE9")    # indigo-100
_GRP_FG    = QColor("#1A237E")    # indigo-900
_GRP_BD    = QColor("#9FA8DA")    # indigo-300


# ─────────────────────────────── delegates ───────────────────────────────────

class _NumericDelegate(QStyledItemDelegate):
    """Inline float editor; Enter advances to next row."""

    def __init__(self, parent=None, decimals: int = 4, active_cols: set = None):
        super().__init__(parent)
        self.decimals   = decimals
        self.active_cols: set = active_cols or set()

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
                cur  = view.currentIndex()
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
    FMT = "yyyy-MM-dd HH:mm"

    def __init__(self, parent=None, active_cols: set = None):
        super().__init__(parent)
        self.active_cols: set = active_cols or set()

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


class _GasNetDelegate(QStyledItemDelegate):
    """Colour-code gas net column: green ≥ 0, red < 0."""
    _GREEN = QColor("#c8e6c9")
    _RED   = QColor("#ffcdd2")
    _GRAY  = QColor("#f5f5f5")

    def paint(self, painter, option, index):
        txt = index.data(Qt.DisplayRole) or ""
        try:
            val = float(txt)
            bg  = self._GREEN if val >= 0 else self._RED
        except ValueError:
            bg  = self._GRAY
        painter.save()
        painter.fillRect(option.rect, QBrush(bg))
        painter.restore()
        super().paint(painter, option, index)




# ─────────────────────────────── grouped header ──────────────────────────────

class _GroupHeaderView(QHeaderView):
    """Two-row header: top = group spans, bottom = column names."""
    _GROUPS = [
        (None,                     list(range(0, 4))),    # Pos…Sample
        (None,                     list(range(4, 6))),    # Times
        ("Tube Weights (g)",       list(range(6, 9))),    # Tube
        ("Gas Bulb Weights (g)",   list(range(9, 12))),   # Gas
        ("Water Bulb Weights (g)", list(range(12, 15))),  # Water
        ("Leak Test",              list(range(15, 17))),  # Leak
        (None,                     list(range(17, 20))),  # Status…Remarks
        ("Extraction Correction",  list(range(20, 25))),  # Temp…Vol
    ]
    _ROW_H = 20

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
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
            super().paintSection(painter, rect, logical_index)
            return
        label, first_col, last_col = ginfo
        bot_rect = QRect(rect.x(), rect.y() + self._ROW_H,
                         rect.width(), rect.height() - self._ROW_H)
        super().paintSection(painter, bot_rect, logical_index)
        x1 = self.sectionViewportPosition(first_col)
        x2 = self.sectionViewportPosition(last_col) + self.sectionSize(last_col)
        top_rect = QRect(x1, rect.y(), x2 - x1, self._ROW_H)
        painter.save()
        painter.setClipping(False)
        painter.fillRect(top_rect, _GRP_BG)
        painter.setPen(_GRP_BD)
        painter.drawRect(top_rect.adjusted(0, 0, -1, -1))
        f = painter.font(); f.setBold(True); painter.setFont(f)
        painter.setPen(_GRP_FG)
        painter.drawText(top_rect, Qt.AlignCenter, label)
        painter.restore()


# ─────────────────────────────── main window ─────────────────────────────────

class NGAMExtractionRunWindow(QDialog):
    """
    Master-detail dialog for a single Noble Gas Extraction Run.
    Header = run metadata; Table = per-sample gravimetric measurements.
    """

    def __init__(self, run_id: int, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)
        self._run_id    = run_id
        self.is_editing = False
        self.has_write  = False
        self._updating  = False
        self._pos_map: dict = {}   # row → iposition (for UPDATE)

        self.setWindowTitle(f"NG Extraction Run  #{run_id}")
        self.resize(1500, 820)
        self.setMinimumSize(1100, 600)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        # delegates
        self._num_active: Set[int] = C.NUMERIC
        self._dt_active:  Set[int] = C.DATETIME

        self._num_delegate = _NumericDelegate(None, decimals=4,
                                              active_cols=self._num_active)
        self._dt_delegate  = _DateTimeDelegate(None, active_cols=self._dt_active)
        self._gas_delegate = _GasNetDelegate()

        # Balance link (RS-232/USB) -- bar only shown while editing. This
        # window's edit flow only bulk-saves on "Stop Editing" (_save_changes
        # validates + writes all rows + recomputes run status), so a balance
        # reading is committed via a separate narrow single-cell UPDATE
        # instead of re-running that full validate/status pass on every
        # weigh-in -- the eventual Stop Editing save still runs normally.
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
            self._load_run()
        except Exception as exc:
            logging.error(f"NGAMExtractionRunWindow init failed: {exc}", exc_info=True)
            self._lbl_footer.setText(f"Error: {exc}")

    # ── privileges ────────────────────────────────────────────────────────────

    def _check_privileges(self):
        try:
            user = normalize_login_name(get_current_user_id())
            self.has_write = check_employee_privilege(user, "ngamaccess")
            self.has_admin = check_employee_privilege(user, "ngamadmin")
        except Exception:
            self.has_write = False
            self.has_admin = False

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # header strip
        hdr_frame = QFrame()
        hdr_frame.setFixedHeight(36)
        hdr_frame.setStyleSheet(f"background:{_HDR_BG};")
        hdr_lay = QHBoxLayout(hdr_frame)
        hdr_lay.setContentsMargins(10, 2, 6, 2)
        hdr_lay.setSpacing(8)
        hdr_title = QLabel(f"Noble Gas Extraction Run   #{self._run_id}")
        hdr_title.setStyleSheet(
            "color:white; font-weight:bold; font-size:13px; background:transparent;"
        )
        hdr_lay.addWidget(hdr_title, 1)
        _help_btn = make_help_button(self, "ngam_line_efficiency")
        _help_btn.setToolTip("Help: Extraction Line Efficiency Calibrations")
        _help_btn.setStyleSheet(
            "QPushButton { background:#1F3A5F; color:white; font-weight:bold;"
            "  border:1px solid #4A7BB5; border-radius:3px;"
            "  padding:2px 8px; font-size:12px; }"
            "QPushButton:hover { background:#2E5F9A; }"
        )
        hdr_lay.addWidget(_help_btn)
        root.addWidget(hdr_frame)

        # body
        body = QVBoxLayout()
        body.setContentsMargins(8, 6, 8, 6)
        body.setSpacing(6)
        root.addLayout(body, 1)

        body.addWidget(self._build_run_header())

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
        body.addWidget(self.balance_bar_w)
        self.btnBalanceRefreshPorts.clicked.connect(self._refresh_balance_ports)
        self.btnBalanceConnect.clicked.connect(self._toggle_balance_connect)

        body.addWidget(self._build_data_group(), 1)

        # footer toolbar
        root.addWidget(self._build_footer())

    # -- run header (6 panels in a grid) --------------------------------------

    def _build_run_header(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame {{ background:{_PANEL_BG}; border:1px solid #AAB8CC; "
            f"border-radius:4px; }}"
        )
        outer = QGridLayout(frame)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        def lbl(t):
            lb = QLabel(t)
            lb.setStyleSheet("font-weight:bold; color:#1F3A5F;")
            lb.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return lb

        def ro(ph=""):
            w = QLineEdit()
            w.setReadOnly(True)
            w.setPlaceholderText(ph)
            w.setStyleSheet("background:white;")
            return w

        def dt_widget():
            w = QDateTimeEdit()
            w.setDisplayFormat("yyyy-MM-dd HH:mm")
            w.setCalendarPopup(True)
            w.setReadOnly(True)
            w.setMinimumDateTime(QDateTime(1900, 1, 1, 0, 0))
            w.setSpecialValueText("—")
            w.setStyleSheet("background:white;")
            return w

        # ── Panel 1: Run Properties ───────────────────────────────────────────
        grp_prop = QGroupBox("Run Properties")
        g_prop = QGridLayout(grp_prop)
        g_prop.setSpacing(4)
        self.cmbProcedure  = QComboBox(); self.cmbProcedure.setEnabled(False)
        self.cmbEquipment  = QComboBox(); self.cmbEquipment.setEnabled(False)
        self.cmbRunStatus  = QComboBox(); self.cmbRunStatus.setEnabled(False)
        g_prop.addWidget(lbl("Procedure:"),        0, 0)
        g_prop.addWidget(self.cmbProcedure,        0, 1)
        g_prop.addWidget(lbl("Extr. System:"),     1, 0)
        g_prop.addWidget(self.cmbEquipment,        1, 1)
        g_prop.addWidget(lbl("Run Status:"),       2, 0)
        g_prop.addWidget(self.cmbRunStatus,        2, 1)
        g_prop.setColumnStretch(1, 1)

        # ── Panel 2: Staff ─────────────────────────────────────────────────────
        grp_staff = QGroupBox("Staff Involved")
        g_staff = QGridLayout(grp_staff)
        g_staff.setSpacing(4)
        self.cmbTechnician = QComboBox(); self.cmbTechnician.setEnabled(False)
        self.chkLocked     = QCheckBox("Locked"); self.chkLocked.setEnabled(False)
        g_staff.addWidget(lbl("Analyst:"),  0, 0)
        g_staff.addWidget(self.cmbTechnician, 0, 1)
        g_staff.addWidget(self.chkLocked,     1, 1)
        g_staff.setColumnStretch(1, 1)

        # ── Panel 3: Remarks ───────────────────────────────────────────────────
        grp_rem = QGroupBox("Remarks")
        g_rem = QVBoxLayout(grp_rem)
        self.txtRemarks = QTextEdit()
        self.txtRemarks.setMaximumHeight(62)
        self.txtRemarks.setReadOnly(True)
        self.txtRemarks.setStyleSheet("background:white;")
        g_rem.addWidget(self.txtRemarks)

        # ── Panel 4: Run Progress ─────────────────────────────────────────────
        grp_prog = QGroupBox("Run Progress")
        g_prog = QGridLayout(grp_prog)
        g_prog.setSpacing(4)

        self.dtStart = dt_widget()
        self.dtEnd   = dt_widget()
        self.txtEstEnd = ro()

        self._btnStampStart = QPushButton("⏱ Now")
        self._btnStampStart.setFixedWidth(60)
        self._btnStampStart.setEnabled(False)
        self._btnStampStart.setToolTip("Stamp current time as Run Start")
        self._btnStampStart.clicked.connect(self._stamp_start_time)

        self._btnStampEnd = QPushButton("⏱ Now")
        self._btnStampEnd.setFixedWidth(60)
        self._btnStampEnd.setEnabled(False)
        self._btnStampEnd.setToolTip("Stamp current time as Run End")
        self._btnStampEnd.clicked.connect(self._stamp_end_time)

        g_prog.addWidget(lbl("Start Time:"), 0, 0)
        g_prog.addWidget(self.dtStart,       0, 1)
        g_prog.addWidget(self._btnStampStart,0, 2)
        g_prog.addWidget(lbl("End Time:"),   1, 0)
        g_prog.addWidget(self.dtEnd,         1, 1)
        g_prog.addWidget(self._btnStampEnd,  1, 2)
        g_prog.addWidget(lbl("Est. End:"),   2, 0)
        g_prog.addWidget(self.txtEstEnd,     2, 1, 1, 2)
        g_prog.setColumnStretch(1, 1)

        # ── Panel 5: Measurement Uncertainties ────────────────────────────────
        grp_unc = QGroupBox("Measurement Uncertainties")
        g_unc = QGridLayout(grp_unc)
        g_unc.setSpacing(4)
        self.txtUncSmall   = ro()
        self.txtUncLarge   = ro()
        self.txtUncPipette = ro()
        g_unc.addWidget(lbl("Small Scale (g):"), 0, 0)
        g_unc.addWidget(self.txtUncSmall,         0, 1)
        g_unc.addWidget(lbl("Large Scale (g):"),  1, 0)
        g_unc.addWidget(self.txtUncLarge,         1, 1)
        g_unc.addWidget(lbl("Pipette (mL):"),     2, 0)
        g_unc.addWidget(self.txtUncPipette,       2, 1)
        g_unc.setColumnStretch(1, 1)

        # ── Panel 6: Date/User Stamps ─────────────────────────────────────────
        grp_stamps = QGroupBox("Date / User Stamps")
        g_stamps = QGridLayout(grp_stamps)
        g_stamps.setSpacing(4)
        self.txtCreateDate  = ro(); self.txtCreateUser  = ro()
        self.txtModifDate   = ro(); self.txtModifUser   = ro()
        g_stamps.addWidget(lbl("Created at:"),  0, 0)
        g_stamps.addWidget(self.txtCreateDate,  0, 1)
        g_stamps.addWidget(lbl("by:"),          0, 2)
        g_stamps.addWidget(self.txtCreateUser,  0, 3)
        g_stamps.addWidget(lbl("Last modif.:"), 1, 0)
        g_stamps.addWidget(self.txtModifDate,   1, 1)
        g_stamps.addWidget(lbl("by:"),          1, 2)
        g_stamps.addWidget(self.txtModifUser,   1, 3)
        g_stamps.setColumnStretch(1, 1); g_stamps.setColumnStretch(3, 1)

        # arrange panels 3 × 2
        outer.addWidget(grp_prop,   0, 0)
        outer.addWidget(grp_staff,  0, 1)
        outer.addWidget(grp_rem,    0, 2)
        outer.addWidget(grp_prog,   1, 0)
        outer.addWidget(grp_unc,    1, 1)
        outer.addWidget(grp_stamps, 1, 2)
        outer.setColumnStretch(0, 2)
        outer.setColumnStretch(1, 2)
        outer.setColumnStretch(2, 3)
        return frame

    # -- data grid ------------------------------------------------------------

    def _build_data_group(self) -> QGroupBox:
        grp = QGroupBox("Extraction Data  (double-click cell to edit in edit mode)")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(4, 4, 4, 4)

        self._model = QStandardItemModel()
        self._model.setHorizontalHeaderLabels(C.HEADERS)
        self._model.dataChanged.connect(self._on_data_changed)

        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setHorizontalHeader(_GroupHeaderView(self._table))
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSortingEnabled(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setStyleSheet("""
            QTableView { border:none; background:white; }
            QTableView::item { padding:3px 5px; }
            QTableView::item:selected { background:#DDEEFF; color:#000; }
            QHeaderView::section {
                background-color:#1F3A5F; color:white;
                font-weight:bold; padding:3px 5px;
                border:1px solid #0F2A4F;
            }
        """)

        self._table.setItemDelegateForColumn(C.GAS_NET, self._gas_delegate)
        lay.addWidget(self._table, 1)
        return grp

    # -- footer ---------------------------------------------------------------

    def _build_footer(self) -> QFrame:
        frm = QFrame()
        frm.setStyleSheet(
            f"background:{_PANEL_BG}; border-top:1px solid #AAB8CC;"
        )
        lay = QHBoxLayout(frm)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)

        self._lbl_footer = QLabel("")
        self._lbl_footer.setStyleSheet("color:#444;")
        lay.addWidget(self._lbl_footer, 1)

        def _btn(label, color, hover=None, tip=""):
            b = QPushButton(label)
            b.setFixedHeight(30)
            b.setToolTip(tip)
            hov = hover or color
            b.setStyleSheet(f"""
                QPushButton {{ background:{color}; color:white; font-weight:bold;
                               border:none; padding:4px 14px; border-radius:4px; }}
                QPushButton:hover {{ background:{hov}; }}
                QPushButton:disabled {{ background:#aab8cc; color:#7f8c8d; }}
            """)
            return b

        self._btnEdit       = _btn("Edit Run",        "#2E7D32", "#1B5E20", "Toggle edit mode")
        self._btnFinalize   = _btn("Finalize",        "#1565C0", "#0D47A1", "Stamp EndTime and mark run complete")
        self._btnExport     = _btn("Export XLSX",     "#6A1B9A", "#4A148C", "Export this run to Excel")
        self._btnBulkStart  = _btn("Stamp All Start", "#E65100", "#BF360C", "Set dTimeStart of all samples to now")
        self._btnBulkEnd    = _btn("Stamp All End",   "#E65100", "#BF360C", "Set dTimeEnd of all samples to now")
        self._btnEfficiency = _btn("Line Efficiency", "#4527A0", "#311B92",
                                   "Manage extraction line efficiency calibrations")
        self._btnPrintLabels = _btn("Print Labels",   "#0288D1", "#01579B", "Print QR code labels for this run")
        self._btnClose      = _btn("Close",           "#7f8c8d", "#636e72")

        self._btnEdit.clicked.connect(self._toggle_edit)
        self._btnFinalize.clicked.connect(self._finalize_run)
        self._btnExport.clicked.connect(self._export_xlsx)
        self._btnBulkStart.clicked.connect(self._bulk_stamp_start)
        self._btnBulkEnd.clicked.connect(self._bulk_stamp_end)
        self._btnEfficiency.clicked.connect(self._open_efficiency_dialog)
        self._btnPrintLabels.clicked.connect(self._print_extraction_labels)
        self._btnClose.clicked.connect(self.reject)

        for b in (self._btnEdit, self._btnFinalize, self._btnExport,
                  self._btnBulkStart, self._btnBulkEnd,
                  self._btnEfficiency, self._btnPrintLabels, self._btnClose):
            lay.addWidget(b)

        return frm

    def _print_extraction_labels(self):
        """Fetch samples for this extraction run and open the QR label printer."""
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT d.iposition,
                           a.prefix, a.sampleid, s.sname,
                           d.analysisid
                    FROM   ngam.ngextractiondata d
                    JOIN   public.analysis a ON a.analysisid = d.analysisid
                    JOIN   public.sample   s ON s.sampleid   = a.sampleid
                                             AND s.prefix     = a.prefix
                    WHERE  d.runid = :r
                    ORDER  BY d.iposition
                """), {"r": self._run_id}).fetchall()
        except Exception as exc:
            logging.error("Extraction label fetch: %s", exc)
            show_message(self, "Error", str(exc), QMessageBox.Critical)
            return
        if not rows:
            show_message(self, "No Data", "No samples found for this run.", QMessageBox.Warning)
            return
        specs = [
            analysis_spec(
                prefix=r.prefix or "",
                sample_id=str(r.sampleid),
                analysis_id=str(r.analysisid),
                container_num=f"Pos {r.iposition}",
                job_type="Extraction",
            )
            for r in rows
        ]
        SampleLabelDialog.show_batch(specs, parent=self).exec_()

    # ── data loading ──────────────────────────────────────────────────────────

    def _load_run(self):
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT r.runid, r.runstatus, r.islocked,
                           r.runstarttime, r.runendtime, r.remarks,
                           r.procedureid, r.equipmentid, r.technicianid,
                           r.createdatestamp, r.createuserstamp,
                           r.modifdatestamp,  r.modifuserstamp,
                           ap.procedurename,
                           eq.equipmentname,
                           e.lastname || ', ' || e.firstmiddlename
                    FROM ngam.ngextractionrun r
                    LEFT JOIN public.analysisprocedure ap ON ap.procedureid = r.procedureid
                    LEFT JOIN public.equipment         eq ON eq.equipmentid = r.equipmentid
                    LEFT JOIN public.employee          e  ON e.employeeid   = r.technicianid
                    WHERE r.runid = :rid
                """), {"rid": self._run_id}).fetchone()

                procedures  = conn.execute(text("""
                    SELECT procedureid,
                           procedureid::text || ' — ' || procedurename
                    FROM public.analysisprocedure
                    WHERE isobsolete = FALSE ORDER BY procedureid
                """)).fetchall()
                equipment = conn.execute(text("""
                    SELECT equipmentid,
                           equipmentid::text || ' — ' || equipmentname
                    FROM public.equipment
                    WHERE categoryid > 11 ORDER BY equipmentid
                """)).fetchall()
                technicians = conn.execute(text("""
                    SELECT employeeid,
                           upper(lastname) || ', ' || firstmiddlename
                    FROM public.employee ORDER BY lastname, firstmiddlename
                """)).fetchall()
                statuses = conn.execute(text("""
                    SELECT status, description
                    FROM public.statuslookup
                    WHERE status IN (4,5) ORDER BY status
                """)).fetchall()

        except Exception as exc:
            logging.error(f"Load extraction run {self._run_id}: {exc}")
            show_message(self, "Error", str(exc), QMessageBox.Critical)
            return

        if not row:
            show_message(self, "Not Found",
                         f"Extraction Run {self._run_id} not found.", QMessageBox.Warning)
            return

        row = tuple(row)
        # -- populate combos --------------------------------------------------
        for cmb, items, cur_id in [
            (self.cmbProcedure,  procedures,  row[6]),
            (self.cmbEquipment,  equipment,   row[7]),
            (self.cmbTechnician, technicians, row[8]),
        ]:
            cmb.blockSignals(True)
            cmb.clear()
            cmb.addItem("—", None)
            sel = 0
            for i, (eid, ename) in enumerate(items, 1):
                cmb.addItem(str(ename), eid)
                if eid == cur_id:
                    sel = i
            cmb.setCurrentIndex(sel)
            cmb.blockSignals(False)

        self.cmbRunStatus.blockSignals(True)
        self.cmbRunStatus.clear()
        for sid, sdesc in statuses:
            self.cmbRunStatus.addItem(f"{sid} — {sdesc}", sid)
        cur_status = row[1]
        for i in range(self.cmbRunStatus.count()):
            if self.cmbRunStatus.itemData(i) == cur_status:
                self.cmbRunStatus.setCurrentIndex(i); break
        self.cmbRunStatus.blockSignals(False)

        # -- header fields ----------------------------------------------------
        self.chkLocked.setChecked(bool(row[2]))
        self._is_locked = bool(row[2])

        def _fmt_dt(v):
            return v.strftime("%Y-%m-%d %H:%M") if v else ""

        if row[3]:
            self.dtStart.setDateTime(
                QDateTime(row[3].year, row[3].month, row[3].day, row[3].hour, row[3].minute))
        else:
            self.dtStart.setDateTime(self.dtStart.minimumDateTime())

        if row[4]:
            self.dtEnd.setDateTime(
                QDateTime(row[4].year, row[4].month, row[4].day, row[4].hour, row[4].minute))
        else:
            self.dtEnd.setDateTime(self.dtEnd.minimumDateTime())

        self.txtRemarks.setPlainText(row[5] or "")

        self.txtCreateDate.setText(_fmt_dt(row[9]))
        self.txtCreateUser.setText(row[10] or "")
        self.txtModifDate.setText(_fmt_dt(row[11]))
        self.txtModifUser.setText(row[12] or "")

        # -- uncertainties from config ----------------------------------------
        try:
            u_small  = get_global_value("BALANCE_UNCERTAINTY_SMALL", 0.001)
            u_large  = get_global_value("BALANCE_UNCERTAINTY_LARGE", 0.002)
            u_pip    = get_global_value("PIPETTE_UNCERTAINTY",        0.01)
            self.txtUncSmall.setText(f"{float(u_small):.4f}")
            self.txtUncLarge.setText(f"{float(u_large):.4f}")
            self.txtUncPipette.setText(f"{float(u_pip):.4f}")
        except Exception:
            pass

        is_complete = row[4] is not None
        self.setWindowTitle(
            f"NG Extraction Run  #{self._run_id}"
            + ("  [COMPLETE]" if is_complete else "  [Ongoing]")
        )

        self._apply_edit_mode(False)
        self._load_data()

    def _load_data(self):
        self._updating = True
        self._model.removeRows(0, self._model.rowCount())
        self._pos_map.clear()

        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT
                        d.iposition,
                        d.analysisid,
                        a.prefix || '-' || a.sampleid::text  AS labid,
                        COALESCE(s.sname, '')               AS sname,
                        d.dtimestart,
                        d.dtimeend,
                        d.fweighttubebefore,
                        d.fweighttubeafter,
                        d.fweightgasbulbbefore,
                        d.fweightgasbulbafter,
                        d.fweightwaterbulbbefore,
                        d.fweightwaterbulbafter,
                        d.fstaticleaktestbefore,
                        d.fleaktestafter,
                        d.status,
                        d.isignored,
                        COALESCE(d.nvcremarks, ''),
                        d.temperature_c,
                        d.lab_pressure_torr,
                        d.salinity_ppt,
                        d.altitude_m,
                        d.extraction_efficiency,
                        COALESCE(s.container_type, 1),
                        d.sample_volume_ml
                    FROM ngam.ngextractiondata d
                    JOIN public.analysis a ON a.analysisid = d.analysisid
                    JOIN public.sample   s ON s.sampleid  = a.sampleid
                                         AND s.prefix     = a.prefix
                    WHERE d.runid = :rid
                    ORDER BY d.iposition
                """), {"rid": self._run_id}).fetchall()

        except Exception as exc:
            logging.error(f"Load extraction data: {exc}")
            show_message(self, "Error", str(exc), QMessageBox.Critical)
            self._updating = False
            return

        def _g(v, fmt=".4f"):
            return f"{v:{fmt}}" if v is not None else ""

        def _dt_s(v):
            return v.strftime("%Y-%m-%d %H:%M") if v else ""

        def _net(a, b, sign=1):
            if a is None or b is None:
                return ""
            return f"{sign * (a - b):.4f}"

        for row_idx, r in enumerate(rows):
            r = tuple(r)
            pos, aid, labid, sname = r[0], r[1], r[2], r[3]
            t_start, t_end = r[4], r[5]
            tube_bef, tube_aft = r[6], r[7]
            gas_bef,  gas_aft  = r[8], r[9]
            wat_bef,  wat_aft  = r[10], r[11]
            leak_bef, leak_aft = r[12], r[13]
            status, ignored, remarks = r[14], r[15], r[16]
            temp_c, lab_pressure, salinity, altitude, efficiency = r[17], r[18], r[19], r[20], r[21]
            container_type = int(r[22]) if r[22] is not None else 1
            sample_volume  = r[23]

            tube_net  = _net(tube_bef, tube_aft, +1)  # before − after
            gas_net   = _net(gas_aft,  gas_bef,  +1)  # after  − before (positive = gas collected)
            # Effective water mass — formula depends on container type from public.sample:
            #   1 = water-bulb:        bulb fills up →  after − before
            #   2 = cu-tube:           tube empties  → before − after
            #   3 = diffusion sampler: use sample_volume_ml directly
            if container_type == 1:
                water_net = _net(wat_aft, wat_bef, +1)
                water_net_val = (wat_aft - wat_bef) if (wat_bef is not None and wat_aft is not None) else None
            elif container_type == 2:
                water_net = _net(tube_bef, tube_aft, +1)
                water_net_val = (tube_bef - tube_aft) if (tube_bef is not None and tube_aft is not None) else None
            elif container_type == 3:
                water_net = f"{sample_volume:.4f}" if sample_volume is not None else ""
                water_net_val = sample_volume
            else:
                water_net = ""
                water_net_val = None

            def _item(text, editable=False, uval=None):
                it = QStandardItem(str(text))
                it.setEditable(editable)
                if uval is not None:
                    it.setData(uval, Qt.UserRole)
                return it

            items = [
                _item(str(pos or "")),                              # POS
                _item(str(aid or "")),                              # AID
                _item(labid),                                       # LABID
                _item(sname),                                       # SNAME
                _item(_dt_s(t_start), editable=True,               # T_START
                      uval=t_start),
                _item(_dt_s(t_end),   editable=True,               # T_END
                      uval=t_end),
                _item(_g(tube_bef),   editable=True,               # TUBE_BEF
                      uval=tube_bef),
                _item(_g(tube_aft),   editable=True,               # TUBE_AFT
                      uval=tube_aft),
                _item(tube_net),                                    # TUBE_NET (r/o)
                _item(_g(gas_bef),    editable=True,               # GAS_BEF
                      uval=gas_bef),
                _item(_g(gas_aft),    editable=True,               # GAS_AFT
                      uval=gas_aft),
                _item(gas_net),                                     # GAS_NET (r/o)
                _item(_g(wat_bef),    editable=True,               # WATER_BEF
                      uval=wat_bef),
                _item(_g(wat_aft),    editable=True,               # WATER_AFT
                      uval=wat_aft),
                _item(water_net, uval=container_type),              # WATER_NET (r/o); UserRole = container_type
                _item(_g(leak_bef),   editable=True,               # LEAK_BEF
                      uval=leak_bef),
                _item(_g(leak_aft),   editable=True,               # LEAK_AFT
                      uval=leak_aft),
                _item(str(status or "")),                           # STATUS
                _item("Yes" if ignored else "No"),                  # IGNORED
                _item(remarks or "", editable=True),                # REMARKS
                _item(_g(temp_c, ".1f"),     editable=True,        # TEMP_C
                      uval=temp_c),
                _item(_g(lab_pressure, ".2f"), editable=True,     # LAB_PRESS
                      uval=lab_pressure),
                _item(_g(salinity, ".2f"),   editable=True,        # SALINITY
                      uval=salinity),
                _item(_g(altitude, ".0f"),   editable=True,        # ALTITUDE
                      uval=altitude),
                _item(_g(efficiency, ".3f"), editable=True,        # EFFICIENCY
                      uval=efficiency),
                _item(_g(sample_volume, ".2f"), editable=True,     # SAMPLE_VOL
                      uval=sample_volume),
            ]

            if ignored:
                for it in items:
                    it.setBackground(QBrush(QColor(230, 230, 230)))

            self._pos_map[row_idx] = pos
            self._model.appendRow(items)

        self._updating = False
        self._resize_columns()
        self._lbl_footer.setText(
            f"{self._model.rowCount()} sample(s) in extraction run."
        )

    def _resize_columns(self):
        h = self._table.horizontalHeader()
        fixed_wide   = {C.T_START, C.T_END}
        fixed_narrow = {C.POS, C.STATUS, C.IGNORED}
        for col in range(self._model.columnCount()):
            if col in fixed_wide:
                h.resizeSection(col, 130)
            elif col in fixed_narrow:
                h.resizeSection(col, 55)
            elif col == C.REMARKS:
                h.setSectionResizeMode(col, QHeaderView.Stretch)
            else:
                h.resizeSection(col, 90)

    # ── derived columns on data change ────────────────────────────────────────

    def _on_data_changed(self, top_left, bottom_right, roles):
        if self._updating:
            return
        self._updating = True
        try:
            for row in range(top_left.row(), bottom_right.row() + 1):
                self._recompute_row(row)
        finally:
            self._updating = False

    def _recompute_row(self, row: int):
        def _fval(col) -> Optional[float]:
            v = self._model.item(row, col)
            if v is None:
                return None
            u = v.data(Qt.UserRole)
            if u is not None:
                try:
                    return float(u)
                except (TypeError, ValueError):
                    pass
            try:
                return float(v.text())
            except (TypeError, ValueError):
                return None

        def _set(col, val):
            it = self._model.item(row, col)
            if it:
                it.setText(f"{val:.4f}" if val is not None else "")

        tube_bef = _fval(C.TUBE_BEF); tube_aft = _fval(C.TUBE_AFT)
        gas_bef  = _fval(C.GAS_BEF);  gas_aft  = _fval(C.GAS_AFT)
        wat_bef  = _fval(C.WATER_BEF);wat_aft  = _fval(C.WATER_AFT)

        # Container type is stored in WATER_NET's UserRole (read from public.sample at load time)
        wnet_item = self._model.item(row, C.WATER_NET)
        container_type = int(wnet_item.data(Qt.UserRole) or 1) if wnet_item else 1

        tube_net = (tube_bef - tube_aft) if (tube_bef is not None and tube_aft is not None) else None
        gas_net  = (gas_aft  - gas_bef)  if (gas_bef  is not None and gas_aft  is not None) else None

        if container_type == 1:
            water_net = (wat_aft - wat_bef) if (wat_bef is not None and wat_aft is not None) else None
        elif container_type == 2:
            water_net = tube_net
        elif container_type == 3:
            water_net = _fval(C.SAMPLE_VOL)
        else:
            water_net = None

        _set(C.TUBE_NET,  tube_net)
        _set(C.GAS_NET,   gas_net)
        _set(C.WATER_NET, water_net)

    # ── edit mode ─────────────────────────────────────────────────────────────

    def _toggle_edit(self):
        if not self.is_editing:
            self._enter_edit_mode()
        else:
            self._exit_edit_mode()

    def _enter_edit_mode(self):
        if not self.has_write:
            show_message(self, "No Permission",
                         "You do not have write privileges for this module.",
                         QMessageBox.Warning)
            return

        self.is_editing = True
        self._apply_edit_mode(True)

    def _exit_edit_mode(self):
        reply = QMessageBox.question(
            self, "Save Changes",
            "Save your edits to this run?\n\n"
            "Yes = save and close edit mode\n"
            "No = discard and close edit mode\n"
            "Cancel = continue editing",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply == QMessageBox.Cancel:
            return
        if reply == QMessageBox.Yes:
            if not self._save_changes():
                return
        self.is_editing = False
        self._apply_edit_mode(False)
        self._load_run()

    def _apply_edit_mode(self, editing: bool):
        self.is_editing = editing

        # header combos
        for cmb in (self.cmbProcedure, self.cmbEquipment,
                    self.cmbTechnician, self.cmbRunStatus):
            cmb.setEnabled(editing)

        self.txtRemarks.setReadOnly(not editing)
        self.dtStart.setReadOnly(not editing)
        self.dtEnd.setReadOnly(not editing)

        self._btnStampStart.setEnabled(editing)
        self._btnStampEnd.setEnabled(editing)
        self._btnBulkStart.setEnabled(editing)
        self._btnBulkEnd.setEnabled(editing)

        # table editing
        if editing:
            self._table.setEditTriggers(
                QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
            )
            self._table.setItemDelegate(self._num_delegate)
            self._table.setItemDelegateForColumn(C.T_START, self._dt_delegate)
            self._table.setItemDelegateForColumn(C.T_END,   self._dt_delegate)
            self._table.setItemDelegateForColumn(C.GAS_NET, self._gas_delegate)
        else:
            self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self._table.setItemDelegateForColumn(C.GAS_NET, self._gas_delegate)

        # read-only columns always locked
        for col in C.READ_ONLY:
            for row in range(self._model.rowCount()):
                it = self._model.item(row, col)
                if it:
                    it.setEditable(False)

        self._btnEdit.setText("Stop Editing" if editing else "Edit Run")
        self._btnEdit.setStyleSheet(
            f"""QPushButton {{ background:{'#C62828' if editing else '#2E7D32'};
                color:white; font-weight:bold; border:none;
                padding:4px 14px; border-radius:4px; }}"""
        )

        if editing:
            self._refresh_balance_ports()
        else:
            self.balance_reader.disconnect_port()
        self.balance_bar_w.setVisible(editing)

        is_complete = (self.dtEnd.dateTime() != self.dtEnd.minimumDateTime())
        self._btnFinalize.setEnabled(
            self.has_write and not is_complete and not editing
        )

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
        injectBalanceValue()/advanceFocus(). Writes straight to the DB via
        _commit_extraction_cell_immediately() since this window's normal
        save path only runs in bulk on Stop Editing."""
        if not self.is_editing:
            return
        idx = self._table.currentIndex()
        if not idx.isValid() or idx.column() not in C.BALANCE_TARGET_COLS:
            return
        row, col = idx.row(), idx.column()
        if row >= self._model.rowCount():
            return

        self._model.item(row, col).setText(f"{value:.4f}")
        self._commit_extraction_cell_immediately(row, col, value)

        next_row = (row + 1) % self._model.rowCount()
        next_idx = self._model.index(next_row, col)
        self._table.setCurrentIndex(next_idx)
        self._table.scrollTo(next_idx)

    def _commit_extraction_cell_immediately(self, row: int, col: int, value: float):
        db_col = C.BALANCE_TARGET_COLS.get(col)
        pos = self._pos_map.get(row)
        if db_col is None or pos is None:
            return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text(f"""
                    UPDATE ngam.ngextractiondata
                    SET {db_col} = :val
                    WHERE runid = :rid AND iposition = :pos
                """), {"val": value, "rid": self._run_id, "pos": pos})
                conn.commit()
        except Exception as exc:
            logging.error("Balance immediate commit failed (row %d): %s", row, exc)
            self._lbl_footer.setText(f"Balance commit failed: {exc}")

    def closeEvent(self, event):
        self.balance_reader.disconnect_port()
        super().closeEvent(event)

    # ── save -----------------------------------------------------------------

    def _save_changes(self) -> bool:
        """Validate and persist all edited rows. Returns True if successful."""
        errors = []
        rows_data = []

        for row in range(self._model.rowCount()):
            pos = self._pos_map.get(row)

            def _fv(col):
                it = self._model.item(row, col)
                if it is None:
                    return None
                u = it.data(Qt.UserRole)
                if u is not None:
                    try:
                        return float(u)
                    except (TypeError, ValueError):
                        pass
                try:
                    return float(it.text())
                except (TypeError, ValueError):
                    return None

            def _dtv(col):
                it = self._model.item(row, col)
                if it is None:
                    return None
                u = it.data(Qt.UserRole)
                if isinstance(u, datetime):
                    return u
                txt = it.text().strip()
                if not txt:
                    return None
                try:
                    return datetime.strptime(txt, "%Y-%m-%d %H:%M")
                except ValueError:
                    return None

            def _sv(col):
                it = self._model.item(row, col)
                return it.text().strip() if it else ""

            t_start = _dtv(C.T_START)
            t_end   = _dtv(C.T_END)
            gas_bef = _fv(C.GAS_BEF)
            gas_aft = _fv(C.GAS_AFT)

            if t_start is None:
                errors.append(f"Row {row+1} (pos {pos}): Start time is required.")

            if t_start and t_end and t_end <= t_start:
                errors.append(
                    f"Row {row+1} (pos {pos}): End time must be after start time."
                )

            rows_data.append({
                "pos":        pos,
                "t_start":    t_start,
                "t_end":      t_end,
                "tube_bef":   _fv(C.TUBE_BEF),
                "tube_aft":   _fv(C.TUBE_AFT),
                "gas_bef":    gas_bef,
                "gas_aft":    gas_aft,
                "wat_bef":    _fv(C.WATER_BEF),
                "wat_aft":    _fv(C.WATER_AFT),
                "leak_bef":   _fv(C.LEAK_BEF),
                "leak_aft":   _fv(C.LEAK_AFT),
                "remarks":    _sv(C.REMARKS),
                "temp_c":     _fv(C.TEMP_C),
                "salinity":   _fv(C.SALINITY),
                "altitude":   _fv(C.ALTITUDE),
                "efficiency": _fv(C.EFFICIENCY),
                "sample_vol": _fv(C.SAMPLE_VOL),
                "lab_press":  _fv(C.LAB_PRESS),
            })

        if errors:
            show_message(self, "Validation Failed",
                         "\n".join(errors), QMessageBox.Warning)
            return False

        # compute status per row
        now = datetime.now()
        all_complete = True
        for rd in rows_data:
            if rd["t_end"] is None or rd["t_start"] is None:
                rd["status"]   = 4
                rd["ignored"]  = 0
                all_complete   = False
            else:
                gas_net = None
                if rd["gas_bef"] is not None and rd["gas_aft"] is not None:
                    gas_net = rd["gas_aft"] - rd["gas_bef"]
                if gas_net is not None and gas_net <= 0:
                    rd["status"]  = 5
                    rd["ignored"] = 1
                    rem = rd["remarks"] or ""
                    if "no gas after extraction" not in rem:
                        rd["remarks"] = (rem + ", no gas after extraction").lstrip(", ")
                else:
                    rd["status"]  = 5
                    rd["ignored"] = 0

        run_status   = 5 if all_complete else 4
        run_end_time = now if all_complete else None

        # run header fields
        proc_id  = self.cmbProcedure.currentData()
        equip_id = self.cmbEquipment.currentData()
        tech_id  = self.cmbTechnician.currentData()
        remarks  = self.txtRemarks.toPlainText().strip() or None
        user     = getpass.getuser()

        try:
            with db_manager.get_connection() as conn:
                # update run header
                conn.execute(text("""
                    UPDATE ngam.ngextractionrun
                    SET procedureid    = :proc,
                        equipmentid    = :equip,
                        technicianid   = :tech,
                        remarks        = :rem,
                        runstatus      = :rstatus,
                        runendtime     = :rend,
                        modifdatestamp = :now,
                        modifuserstamp = :usr
                    WHERE runid = :rid
                """), {
                    "proc": proc_id, "equip": equip_id, "tech": tech_id,
                    "rem": remarks, "rstatus": run_status,
                    "rend": run_end_time, "now": now, "usr": user,
                    "rid": self._run_id,
                })

                # update each sample row
                for rd in rows_data:
                    conn.execute(text("""
                        UPDATE ngam.ngextractiondata
                        SET dtimestart             = :t_start,
                            dtimeend               = :t_end,
                            fweighttubebefore      = :tube_bef,
                            fweighttubeafter       = :tube_aft,
                            fweightgasbulbbefore   = :gas_bef,
                            fweightgasbulbafter    = :gas_aft,
                            fweightwaterbulbbefore = :wat_bef,
                            fweightwaterbulbafter  = :wat_aft,
                            fstaticleaktestbefore  = :leak_bef,
                            fleaktestafter         = :leak_aft,
                            status                 = :rstatus,
                            isignored              = :ignored,
                            nvcremarks             = :rem,
                            temperature_c          = :temp_c,
                            salinity_ppt           = :salinity,
                            altitude_m             = :altitude,
                            extraction_efficiency  = :efficiency,
                            sample_volume_ml       = :sample_vol,
                            lab_pressure_torr      = :lab_press
                        WHERE runid = :rid AND iposition = :pos
                    """), {
                        "t_start":   rd["t_start"],    "t_end":    rd["t_end"],
                        "tube_bef":  rd["tube_bef"],   "tube_aft": rd["tube_aft"],
                        "gas_bef":   rd["gas_bef"],    "gas_aft":  rd["gas_aft"],
                        "wat_bef":   rd["wat_bef"],    "wat_aft":  rd["wat_aft"],
                        "leak_bef":  rd["leak_bef"],   "leak_aft": rd["leak_aft"],
                        "rstatus":   rd["status"],     "ignored":  rd["ignored"],
                        "rem":       rd["remarks"] or None,
                        "temp_c":     rd["temp_c"],      "salinity":   rd["salinity"],
                        "altitude":   rd["altitude"],    "efficiency": rd["efficiency"],
                        "sample_vol": rd["sample_vol"],  "lab_press":  rd["lab_press"],
                        "rid": self._run_id, "pos": rd["pos"],
                    })

                conn.commit()

        except Exception as exc:
            logging.error(f"Save extraction run failed: {exc}", exc_info=True)
            show_message(self, "Save Failed", str(exc), QMessageBox.Critical)
            return False

        return True

    # ── finalize ──────────────────────────────────────────────────────────────

    def _finalize_run(self):
        reply = QMessageBox.question(
            self, "Finalize Run",
            f"Mark Extraction Run {self._run_id} as complete and lock it?\n\n"
            "This stamps RunEndTime to now, sets RunStatus = 5, and locks the run.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        now = datetime.now()
        try:
            with db_manager.get_connection() as conn:
                # 1. Update status and lock the run
                conn.execute(text("""
                    UPDATE ngam.ngextractionrun
                    SET runendtime = :now, runstatus = 5, islocked = TRUE,
                        modifdatestamp = :now, modifuserstamp = :usr
                    WHERE runid = :rid
                """), {"now": now, "usr": getpass.getuser(), "rid": self._run_id})

                # 2. Collect eligible analysisids (completed, not yet in ngpreparations)
                eligible_aids = [
                    r[0] for r in conn.execute(text("""
                        SELECT ed.analysisid
                        FROM ngam.ngextractiondata ed
                        WHERE ed.runid = :rid
                          AND ed.status = 5
                          AND NOT EXISTS (
                              SELECT 1 FROM ngam.ngpreparations p WHERE p.analysisid = ed.analysisid
                          )
                    """), {"rid": self._run_id}).fetchall()
                ]

                # 3. Resolve extraction workflowjobid (for queue advancement)
                row_wjid = conn.execute(text("""
                    SELECT workflowjobid, workflowid, procedureid
                    FROM ngam.ngextractionrun WHERE runid = :rid
                """), {"rid": self._run_id}).fetchone()

                ext_wjid = None
                if row_wjid:
                    ext_wjid = row_wjid[0]
                    if not ext_wjid and row_wjid[1] and row_wjid[2]:
                        row_fb = conn.execute(text("""
                            SELECT workflowjobid
                            FROM public.workflowjob
                            WHERE workflowid = :wfid AND procedureid = :procid AND COALESCE(isobsolete, FALSE) = FALSE
                            LIMIT 1
                        """), {"wfid": row_wjid[1], "procid": row_wjid[2]}).fetchone()
                        if row_fb:
                            ext_wjid = row_fb[0]

                # 4. Advance eligible samples to the next workflow step queue
                staged = 0
                if eligible_aids and ext_wjid:
                    rows = conn.execute(text("""
                        SELECT analysisid, staged, skip_reason
                        FROM public.sp_stage_forward(:wjid, :aids, 'desktop')
                    """), {"wjid": ext_wjid, "aids": eligible_aids}).fetchall()
                    staged = sum(1 for r in rows if r[1])

                conn.commit()
                logging.info(f"Extraction run {self._run_id} finalized. Staged {staged} samples.")

            self._load_run()
        except Exception as exc:
            logging.error(f"Finalize extraction run failed: {exc}", exc_info=True)
            show_message(self, "Error", str(exc), QMessageBox.Critical)

    # ── stamp all start/end times ─────────────────────────────────────────────

    def _bulk_stamp_start(self):
        now = datetime.now()
        reply = QMessageBox.question(
            self, "Stamp Start Time",
            f"Set dTimeStart of ALL samples to {now.strftime('%Y-%m-%d %H:%M')}?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE ngam.ngextractiondata SET dtimestart = :now
                    WHERE runid = :rid
                """), {"now": now, "rid": self._run_id})
                conn.commit()
            self._load_data()
        except Exception as exc:
            show_message(self, "Error", str(exc), QMessageBox.Critical)

    def _bulk_stamp_end(self):
        now = datetime.now()
        reply = QMessageBox.question(
            self, "Stamp End Time",
            f"Set dTimeEnd of ALL samples to {now.strftime('%Y-%m-%d %H:%M')}?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE ngam.ngextractiondata SET dtimeend = :now
                    WHERE runid = :rid
                """), {"now": now, "rid": self._run_id})
                conn.commit()
            self._load_data()
        except Exception as exc:
            show_message(self, "Error", str(exc), QMessageBox.Critical)

    # ── extraction line efficiency ────────────────────────────────────────────

    def _open_efficiency_dialog(self):
        from ngam_extraction_efficiency_gui import ExtractionEfficiencyDialog
        equip_id = self.cmbEquipment.currentData()
        dlg = ExtractionEfficiencyDialog(
            parent=self,
            equipment_id=equip_id,
            run_id=self._run_id,
        )
        dlg.exec_()

    # ── header time stamps ────────────────────────────────────────────────────

    def _stamp_start_time(self):
        now = QDateTime.currentDateTime()
        self.dtStart.setReadOnly(False)
        self.dtStart.setDateTime(now)
        self.dtStart.setReadOnly(not self.is_editing)

    def _stamp_end_time(self):
        now = QDateTime.currentDateTime()
        self.dtEnd.setReadOnly(False)
        self.dtEnd.setDateTime(now)
        self.dtEnd.setReadOnly(not self.is_editing)

    # ── export ────────────────────────────────────────────────────────────────

    def _export_xlsx(self):
        try:
            import openpyxl
        except ImportError:
            show_message(self, "Missing Package",
                         "Please install openpyxl:  pip install openpyxl",
                         QMessageBox.Warning)
            return

        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Export to Excel",
            f"NG_Extraction_{self._run_id}.xlsx",
            "Excel Files (*.xlsx)",
        )
        if not path:
            return

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = f"Run {self._run_id}"

        headers = C.HEADERS
        ws.append(headers)

        for row in range(self._model.rowCount()):
            r_data = []
            for col in range(self._model.columnCount()):
                it = self._model.item(row, col)
                r_data.append(it.text() if it else "")
            ws.append(r_data)

        try:
            wb.save(path)
            show_message(self, "Export Complete",
                         f"Run saved to:\n{path}", QMessageBox.Information)
        except Exception as exc:
            show_message(self, "Export Failed", str(exc), QMessageBox.Critical)


# ─────────────────────────────── standalone test ─────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    run_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    dlg = NGAMExtractionRunWindow(run_id=run_id)
    dlg.show()
    sys.exit(app.exec_())
