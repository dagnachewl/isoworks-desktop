"""
ngam_sequence_run_gui.py
========================
NGAM – 3He Sequence/Measurement: Single Run Detail Window

Displays the header of a NG3HeSequenceRun and its load list
(NG3HeSequenceLoadList), with basic finalize support.

Schema (ngam.ng3hesequencerun):
    runid, workflowid, workflowjobid, procedureid, equipmentid,
    technicianid, runstarttime, runendtime, runstatus, islocked,
    remarks, createdatestamp, createuserstamp,
    minutescompleted, meanbackground, meanbackgroundunc,
    meanstandard, meanstandardunc, outliermethod,
    datapath, headerstoimport, storedheaders

Schema (ngam.ng3hesequenceloadlist):
    headerid (IDENTITY), runid, analysisid, ingrowthid, precursorid,
    positioninrun, traynumber, positionintray, sampletype, sampleamount,
    measurementtime, status, knownstdactivity, knownstdactivityunc,
    bisblank, bisreproreference, bislinreference,
    isrejected, remarks, createdatestamp, createuserstamp
"""
from __future__ import annotations
import sys
import logging
from typing import Optional

from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QLineEdit, QTextEdit, QDateTimeEdit,
    QPushButton, QTableView, QHeaderView, QTabWidget, QWidget,
    QAbstractItemView, QMessageBox, QCheckBox, QFrame, QComboBox,
)
from PyQt5.QtCore import Qt, QDateTime
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QColor, QBrush

from db_core import db_manager
from sqlalchemy import text
from shared_utils import check_employee_privilege, normalize_login_name, get_current_user_id
from gui_utils import show_message
from validation_gui import QCStatusWidget


# ─────────────────────────────── column constants ────────────────────────────
class C:
    POS        = 0
    AID        = 1
    IID        = 2   # ingrowthid
    LABID      = 3
    SNAME      = 4
    AMOUNT     = 5
    MEAS_TIME  = 6
    ACTIVITY   = 7
    ACT_UNC    = 8
    STATUS     = 9
    REJECTED   = 10
    REMARKS    = 11

    HEADERS = [
        "Inlet", "AnalysisID", "IngrowthID",
        "Lab ID", "Sample Name",
        "Amount", "Meas. Time",
        "Activity", "Act. Unc",
        "Status", "Rejected?", "Remarks",
    ]


class CR:
    """Column indices for the Results tab."""
    POS        = 0
    LABID      = 1
    SNAME      = 2
    TYPE       = 3
    CYCLES     = 4
    BG         = 5
    NET        = 6
    NET_UNC    = 7
    SENS       = 8
    ACT        = 9
    ACT_UNC    = 10
    T_EM       = 11   # ingrowth duration (extraction → measurement), days
    T_SE       = 12   # storage duration  (sampling → extraction), days
    WATER      = 13   # post-degassing water mass, g
    CORR_F     = 14   # ingrowth correction factor
    ACT_CORR   = 15   # activity corrected (TU)
    ACT_CORR_U = 16   # activity corrected uncertainty (TU)
    REJECTED   = 17
    COUNT      = 18

    HEADERS = [
        "Inlet", "Lab ID", "Sample Name", "Type", "Cyc.",
        "BG (A)", "Net ³He (A)", "± Unc",
        "Sensitivity",
        "Activity (ccSTP)", "± Unc",
        "t_em (d)", "t_se (d)", "Water (g)",
        "Corr. Factor",
        "Act. Corr. (TU)", "± Unc",
        "Rejected?",
    ]


def _sfmt(val, sig: int = 5) -> str:
    """Format a float with sig-figs, using scientific notation for small values."""
    if val is None:
        return ""
    try:
        return f"{float(val):.{sig}g}"
    except (TypeError, ValueError):
        return str(val)


# ─────────────────────────────── palette ─────────────────────────────────────
_HDR_BG   = "#1F3A5F"
_PANEL_BG = "#EAF0F6"


# ─────────────────────────────── window ──────────────────────────────────────
class NGAMSequenceRunWindow(QDialog):
    """Read / edit a single 3He Sequence Measurement Run."""

    def __init__(self, run_id: int, parent=None):
        super().__init__(parent)
        self._run_id = run_id
        self._locked = False
        self.setWindowTitle(f"3He Sequence Run  #{run_id}")
        self.resize(1100, 680)
        self.setMinimumSize(900, 520)

        self._check_privileges()
        self._build_ui()
        self._populate_combos()
        self._load_run()

    # ── privileges ────────────────────────────────────────────────────────────

    def _check_privileges(self):
        try:
            user = normalize_login_name(get_current_user_id())
            self.has_write_priv = check_employee_privilege(user, "ngamaccess")
            self.has_admin_priv = check_employee_privilege(user, "ngamadmin")
        except Exception:
            self.has_write_priv = False
            self.has_admin_priv = False

    # ── combo population ──────────────────────────────────────────────────────

    def _populate_combos(self):
        try:
            with db_manager.get_connection() as conn:
                # Status — relevant NGAM MS statuses; cancelled last
                self.cmbStatus.clear()
                rows = conn.execute(text("""
                    SELECT status, description
                    FROM public.statuslookup
                    WHERE status IN (6, 7, 8, 5, 210, -9)
                    ORDER BY CASE WHEN status < 0 THEN 9999 ELSE status END
                """)).fetchall()
                for r in rows:
                    self.cmbStatus.addItem(f"{r[0]}  {r[1]}", r[0])

                # Technician
                self.cmbTech.clear()
                self.cmbTech.addItem("", None)
                rows = conn.execute(text("""
                    SELECT employeeid, lastname, firstmiddlename
                    FROM public.employee
                    ORDER BY lastname, firstmiddlename
                """)).fetchall()
                for r in rows:
                    name = f"{r[1]}, {r[2]}" if r[2] else r[1]
                    self.cmbTech.addItem(name, r[0])
        except Exception as exc:
            logging.error("_populate_combos failed: %s", exc)

    def _on_edit_toggled(self, checked: bool):
        self.cmbStatus.setEnabled(checked)
        self.cmbTech.setEnabled(checked)
        self.txtRemarks.setReadOnly(not checked)
        self.txtRemarks.setStyleSheet(
            "background:white;" if checked else "background:#f8f8f8; color:#555;"
        )
        self.btnSaveHeader.setEnabled(checked)

    def _save_header(self):
        try:
            new_status  = self.cmbStatus.currentData()
            new_tech    = self.cmbTech.currentData()
            new_remarks = self.txtRemarks.toPlainText().strip() or None
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE ngam.msrun
                       SET runstatus    = :status,
                           technicianid = :tech,
                           remarks      = :remarks
                     WHERE runid = :rid AND measurement_mode = 'IG'
                """), {"status": new_status, "tech": new_tech,
                       "remarks": new_remarks, "rid": self._run_id})
                conn.commit()
            self.btnEditHeader.setChecked(False)
            self._on_edit_toggled(False)
            self._load_run()
        except Exception as exc:
            logging.error("_save_header failed: %s", exc)
            show_message(self, "Save Error", str(exc), QMessageBox.Critical)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── header strip ──────────────────────────────────────────────────────
        hdr_strip = QLabel(f"  3He Sequence / Measurement Run   #{self._run_id}")
        hdr_strip.setFixedHeight(30)
        hdr_strip.setStyleSheet(
            f"background:{_HDR_BG}; color:white; font-weight:bold; font-size:13px;"
        )
        root.addWidget(hdr_strip)

        # ── body ──────────────────────────────────────────────────────────────
        body = QVBoxLayout()
        body.setContentsMargins(10, 8, 10, 8)
        body.setSpacing(8)
        root.addLayout(body, 1)

        body.addWidget(self._build_header_group())
        body.addWidget(self._build_data_tabs(), 1)

        # ── footer ────────────────────────────────────────────────────────────
        ftr_w = QFrame()
        ftr_w.setStyleSheet(f"background:{_PANEL_BG}; border-top:1px solid #AAB8CC;")
        ftr = QHBoxLayout(ftr_w)
        ftr.setContentsMargins(10, 6, 10, 6)

        self.lblFooter = QLabel("")
        self.lblFooter.setStyleSheet("color:#444;")

        self.btnQCReview = QPushButton("QC / Validate")
        self.btnQCReview.setFixedHeight(30)
        self.btnQCReview.setMinimumWidth(120)
        self.btnQCReview.setStyleSheet("""
            QPushButton { background:#2980b9; color:white; font-weight:bold;
                          border:none; padding:4px 18px; border-radius:4px; }
            QPushButton:hover { background:#1f618d; }
            QPushButton:disabled { background:#aab8cc; color:#7f8c8d; }
        """)
        self.btnQCReview.clicked.connect(self._open_qc_tab)

        self.btnSaveHeader = QPushButton("Save")
        self.btnSaveHeader.setFixedHeight(30)
        self.btnSaveHeader.setMinimumWidth(80)
        self.btnSaveHeader.setStyleSheet("""
            QPushButton { background:#E67E22; color:white; font-weight:bold;
                          border:none; padding:4px 18px; border-radius:4px; }
            QPushButton:hover { background:#CA6F1E; }
            QPushButton:disabled { background:#aab8cc; color:#7f8c8d; }
        """)
        self.btnSaveHeader.clicked.connect(self._save_header)
        self.btnSaveHeader.setEnabled(False)

        self.btnEditHeader = QPushButton("Edit")
        self.btnEditHeader.setFixedHeight(30)
        self.btnEditHeader.setMinimumWidth(80)
        self.btnEditHeader.setCheckable(True)
        self.btnEditHeader.setStyleSheet("""
            QPushButton { background:#5D6D7E; color:white; font-weight:bold;
                          border:none; padding:4px 18px; border-radius:4px; }
            QPushButton:hover { background:#4A5568; }
            QPushButton:checked { background:#1F3A5F; }
            QPushButton:disabled { background:#aab8cc; color:#7f8c8d; }
        """)
        self.btnEditHeader.toggled.connect(self._on_edit_toggled)

        self.btnImportMS = QPushButton("Import MS Data")
        self.btnImportMS.setFixedHeight(30)
        self.btnImportMS.setMinimumWidth(130)
        self.btnImportMS.setStyleSheet("""
            QPushButton { background:#27AE60; color:white; font-weight:bold;
                          border:none; padding:4px 18px; border-radius:4px; }
            QPushButton:hover { background:#1E8449; }
            QPushButton:disabled { background:#aab8cc; color:#7f8c8d; }
        """)
        self.btnImportMS.clicked.connect(self._open_import_dialog)

        btnClose = QPushButton("Close")
        btnClose.setFixedHeight(30)
        btnClose.setMinimumWidth(80)
        btnClose.setStyleSheet("""
            QPushButton { background:#7f8c8d; color:white; font-weight:bold;
                          border:none; padding:4px 18px; border-radius:4px; }
            QPushButton:hover { background:#636e72; }
        """)
        btnClose.clicked.connect(self.reject)

        ftr.addWidget(self.lblFooter, 1)
        ftr.addWidget(self.btnEditHeader)
        ftr.addSpacing(4)
        ftr.addWidget(self.btnSaveHeader)
        ftr.addSpacing(16)
        ftr.addWidget(self.btnImportMS)
        ftr.addSpacing(8)
        ftr.addWidget(self.btnQCReview)
        ftr.addSpacing(8)
        ftr.addWidget(btnClose)
        root.addWidget(ftr_w)

    def _build_header_group(self) -> QGroupBox:
        grp = QGroupBox("Run Details")
        grp.setStyleSheet(
            f"QGroupBox {{ background:{_PANEL_BG}; }}\n"
            "QDateTimeEdit::drop-down { background: #C5CAE9; border: none; width: 22px; border-radius: 0 6px 6px 0; }\n"
            "QDateTimeEdit::drop-down:hover { background: #7986CB; }\n"
            "QDateTimeEdit::drop-down:pressed { background: #3949AB; }\n"
            "QDateTimeEdit::down-arrow { image: none; width: 0; height: 0; "
            "border-left: 5px solid transparent; border-right: 5px solid transparent; border-top: 6px solid #3949AB; }"
        )
        lay = QGridLayout(grp)
        lay.setVerticalSpacing(5)
        lay.setHorizontalSpacing(10)

        def lbl(t):
            l = QLabel(t)
            l.setStyleSheet("font-weight:bold; color:#1F3A5F;")
            l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return l

        def ro(placeholder=""):
            w = QLineEdit()
            w.setReadOnly(True)
            w.setPlaceholderText(placeholder)
            w.setStyleSheet("background:white;")
            return w

        _cmb_ss = "QComboBox { background:white; } QComboBox:disabled { background:#f0f0f0; color:#888; }"

        self.txtWorkflow  = ro()
        self.txtProcedure = ro()
        self.txtEquipment = ro()
        self.cmbTech      = QComboBox(); self.cmbTech.setStyleSheet(_cmb_ss)
        self.dtStart = QDateTimeEdit(); self.dtStart.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dtStart.setCalendarPopup(True); self.dtStart.setReadOnly(True)
        self.dtStart.setMinimumDateTime(QDateTime(1900, 1, 1, 0, 0))
        self.dtStart.setSpecialValueText("—"); self.dtStart.setStyleSheet("background:white;")
        self.dtEnd = QDateTimeEdit(); self.dtEnd.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dtEnd.setCalendarPopup(True); self.dtEnd.setReadOnly(True)
        self.dtEnd.setMinimumDateTime(QDateTime(1900, 1, 1, 0, 0))
        self.dtEnd.setSpecialValueText("—"); self.dtEnd.setStyleSheet("background:white;")
        self.cmbStatus    = QComboBox(); self.cmbStatus.setStyleSheet(_cmb_ss)
        self.chkLocked    = QCheckBox("Locked")
        self.chkLocked.setEnabled(False)
        self.txtRemarks   = QTextEdit()
        self.txtRemarks.setMaximumHeight(46)
        self.txtRemarks.setReadOnly(True)
        self.txtRemarks.setStyleSheet("background:#f8f8f8; color:#555;")

        # left column
        for row, (label, widget) in enumerate([
            ("Workflow:",   self.txtWorkflow),
            ("Procedure:",  self.txtProcedure),
            ("Device:",     self.txtEquipment),
            ("Technician:", self.cmbTech),
        ]):
            lay.addWidget(lbl(label), row, 0)
            lay.addWidget(widget, row, 1)

        # right column
        for row, (label, widget) in enumerate([
            ("Start Time:", self.dtStart),
            ("End Time:",   self.dtEnd),
            ("Status:",     self.cmbStatus),
            ("Locked:",     self.chkLocked),
        ]):
            lay.addWidget(lbl(label), row, 2)
            lay.addWidget(widget, row, 3)

        lay.addWidget(lbl("Remarks:"), 4, 0)
        lay.addWidget(self.txtRemarks, 4, 1, 1, 3)

        lay.setColumnStretch(1, 1)
        lay.setColumnStretch(3, 1)
        return grp

    def _build_data_tabs(self) -> QTabWidget:
        _tbl_ss = """
            QTableView { border: none; background-color: white; }
            QTableView::item { padding: 4px 6px; }
            QTableView::item:selected { background-color: #DDEEFF; color: #000; }
            QHeaderView::section { background-color: #1F3A5F; color: white;
                                   font-weight: bold; padding: 4px 6px;
                                   border: 1px solid #0F2A4F; }
        """
        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #AAB8CC; }"
            "QTabBar::tab { background: #C5CDD9; padding: 5px 14px; font-weight: bold; min-width: 110px; }"
            "QTabBar::tab:selected { background: #1F3A5F; color: white; }"
        )

        # ── Tab 1: Load List ──────────────────────────────────────────────────
        ll_w = QWidget()
        ll_lay = QVBoxLayout(ll_w)
        ll_lay.setContentsMargins(4, 4, 4, 4)

        self.tblLoadList = QTableView()
        self.tblLoadList.setShowGrid(False)
        self.tblLoadList.setAlternatingRowColors(True)
        self.tblLoadList.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblLoadList.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblLoadList.setSortingEnabled(True)
        self.tblLoadList.verticalHeader().setVisible(False)
        self.tblLoadList.setStyleSheet(_tbl_ss)
        self.listModel = QStandardItemModel()
        self.listModel.setHorizontalHeaderLabels(C.HEADERS)
        self.tblLoadList.setModel(self.listModel)
        ll_lay.addWidget(self.tblLoadList)
        tabs.addTab(ll_w, "Load List")

        # ── Tab 2: Results ────────────────────────────────────────────────────
        res_w = QWidget()
        res_lay = QVBoxLayout(res_w)
        res_lay.setContentsMargins(4, 4, 4, 4)

        self.tblResults = QTableView()
        self.tblResults.setShowGrid(False)
        self.tblResults.setAlternatingRowColors(True)
        self.tblResults.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblResults.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblResults.setSortingEnabled(True)
        self.tblResults.verticalHeader().setVisible(False)
        self.tblResults.setStyleSheet(_tbl_ss)
        self.resultsModel = QStandardItemModel()
        self.resultsModel.setHorizontalHeaderLabels(CR.HEADERS)
        self.tblResults.setModel(self.resultsModel)
        res_lay.addWidget(self.tblResults)
        tabs.addTab(res_w, "Results")

        # ── Tab 3: QC / Validation ────────────────────────────────────────────
        qc_w = QWidget()
        qc_lay = QVBoxLayout(qc_w)
        qc_lay.setContentsMargins(8, 8, 8, 8)
        self._qc_widget = QCStatusWidget(
            module         = "NGAM",
            run_id         = self._run_id,
            parent_refresh = self._load_run,
            parent         = qc_w,
        )
        self._qc_widget.validated.connect(self._load_run)
        qc_lay.addWidget(self._qc_widget)
        self._tabs = tabs
        tabs.addTab(qc_w, "QC / Validation")

        return tabs

    # ── data loading ──────────────────────────────────────────────────────────

    def _load_run(self):
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT
                        r.runid, r.runstatus, r.islocked,
                        r.runstarttime, r.runendtime,
                        r.remarks,
                        w.workflowname,
                        ap.procedurename,
                        eq.equipmentname,
                        r.technicianid
                    FROM ngam.msrun r
                    LEFT JOIN public.workflow          w  ON w.workflowid  = r.workflowid
                    LEFT JOIN public.analysisprocedure ap ON ap.procedureid = r.procedureid
                    LEFT JOIN public.equipment         eq ON eq.equipmentid = r.equipmentid
                    WHERE r.runid = :rid AND r.measurement_mode = 'IG'
                """), {"rid": self._run_id}).fetchone()

            if not row:
                show_message(self, "Not Found",
                             f"Sequence Run {self._run_id} not found.", QMessageBox.Warning)
                return

            self._locked = bool(row[2])
            runstatus   = row[1]
            techid      = row[9]

            self.txtWorkflow.setText(row[6] or "")
            self.txtProcedure.setText(row[7] or "")
            self.txtEquipment.setText(row[8] or "")
            if row[3]:
                self.dtStart.setDateTime(QDateTime(row[3].year, row[3].month, row[3].day, row[3].hour, row[3].minute))
            else:
                self.dtStart.setDateTime(self.dtStart.minimumDateTime())
            if row[4]:
                self.dtEnd.setDateTime(QDateTime(row[4].year, row[4].month, row[4].day, row[4].hour, row[4].minute))
            else:
                self.dtEnd.setDateTime(self.dtEnd.minimumDateTime())
            self.chkLocked.setChecked(self._locked)
            self.txtRemarks.setPlainText(row[5] or "")

            idx = self.cmbStatus.findData(runstatus)
            if idx >= 0:
                self.cmbStatus.setCurrentIndex(idx)
            idx = self.cmbTech.findData(techid)
            if idx >= 0:
                self.cmbTech.setCurrentIndex(idx)

            can_edit = self.has_write_priv and not self._locked
            self.btnEditHeader.setEnabled(can_edit)
            # reset edit mode on reload
            self.btnEditHeader.setChecked(False)
            self._on_edit_toggled(False)

            is_complete = row[4] is not None  # RunEndTime set
            self.btnQCReview.setEnabled(not self._locked)
            self.setWindowTitle(
                f"3He Sequence Run  #{self._run_id}"
                + ("  [COMPLETE]" if is_complete else "  [Ongoing]")
            )

        except Exception as e:
            logging.error(f"Load sequence run {self._run_id} failed: {e}")
            show_message(self, "Error", str(e), QMessageBox.Critical)
            return

        self._load_loadlist()
        self._load_results()

    def _load_loadlist(self):
        self.listModel.removeRows(0, self.listModel.rowCount())
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT
                        sl.positioninrun,
                        sl.analysisid,
                        sl.ingrowthid,
                        a.prefix || '-' || CAST(a.sampleid AS VARCHAR),
                        COALESCE(s.sname, ''),
                        COALESCE(sl.sampleamount, t.freferenceamount) AS sampleamount,
                        sl.measurementtime,
                        COALESCE(res.activity_corrected, res.activity,
                                 sl.knownstdactivity)      AS activity,
                        COALESCE(res.activity_corrected_unc, res.activity_unc,
                                 sl.knownstdactivityunc)   AS activity_unc,
                        sl.status,
                        sl.isrejected,
                        COALESCE(sl.remarks, '')
                    FROM ngam.ng3hesequenceloadlist sl
                    JOIN public.analysis a ON a.analysisid = sl.analysisid
                    JOIN public.sample   s ON s.sampleid = a.sampleid
                                         AND s.prefix    = a.prefix
                    JOIN ngam.msrun r ON r.runid = sl.runid AND r.measurement_mode = 'IG'
                    LEFT JOIN public.ngseqtemplate t
                        ON t.procedureid = r.procedureid
                        AND t.iinletid   = sl.positioninrun
                    LEFT JOIN ngam.ng3hesequenceresults res
                        ON res.runid        = sl.runid
                        AND res.positioninrun = sl.positioninrun
                    WHERE sl.runid = :rid
                    ORDER BY sl.positioninrun
                """), {"rid": self._run_id}).fetchall()

            for r in rows:
                r = tuple(r)
                items = [
                    QStandardItem(str(r[0] or "")),   # Pos
                    QStandardItem(str(r[1] or "")),   # AID
                    QStandardItem(str(r[2] or "")),   # IID
                    QStandardItem(str(r[3] or "")),   # LabID
                    QStandardItem(str(r[4] or "")),   # sName
                    QStandardItem(                    # Amount (sci notation for small values)
                        f"{r[5]:.6g}" if r[5] is not None else ""),
                    QStandardItem(                    # Meas. Time
                        r[6].strftime("%Y-%m-%d %H:%M") if r[6] else ""),
                    QStandardItem(                    # Activity
                        f"{r[7]:.6g}" if r[7] is not None else ""),
                    QStandardItem(                    # Act. Unc
                        f"{r[8]:.6g}" if r[8] is not None else ""),
                    QStandardItem(str(r[9] or "")),   # Status
                    QStandardItem("Yes" if r[10] else "No"),  # Rejected
                    QStandardItem(r[11] or ""),       # Remarks
                ]
                for it in items:
                    it.setEditable(False)
                if r[10]:  # rejected → red tint
                    for it in items:
                        it.setBackground(QBrush(QColor(255, 220, 220)))
                self.listModel.appendRow(items)

            h = self.tblLoadList.horizontalHeader()
            for i in range(len(C.HEADERS) - 1):
                h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
            h.setSectionResizeMode(len(C.HEADERS) - 1, QHeaderView.Stretch)

            self.lblFooter.setText(
                f"{self.listModel.rowCount()} sample(s) in load list."
            )

        except Exception as e:
            logging.error(f"Load sequence loadlist failed: {e}")
            show_message(self, "Error", str(e), QMessageBox.Critical)

    def _load_results(self):
        self.resultsModel.removeRows(0, self.resultsModel.rowCount())
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT
                        res.positioninrun,
                        COALESCE(a.prefix || '-' || CAST(a.sampleid AS VARCHAR), '') AS labid,
                        COALESCE(s.sname, '')                                        AS sname,
                        res.position_type,
                        res.n_cycles_used,
                        res.bg_mean_a,
                        res.net3he_a,
                        res.net3he_unc_a,
                        res.sensitivity,
                        res.activity,
                        res.activity_unc,
                        EXTRACT(EPOCH FROM (ingr.dtimeend - ingr.dtimestart))
                            / 86400.0                                                AS t_em_days,
                        EXTRACT(EPOCH FROM (ingr.dtimestart - s.collectiondate))
                            / 86400.0                                                AS t_se_days,
                        ingr.fweightwaterbulbafter - ingr.fweightwaterbulbempty     AS water_g,
                        res.ingrowth_correction_factor,
                        res.activity_corrected,
                        res.activity_corrected_unc,
                        res.isrejected
                    FROM ngam.ng3hesequenceresults res
                    JOIN ngam.ng3hesequenceloadlist sl
                        ON sl.runid = res.runid
                        AND sl.positioninrun = res.positioninrun
                    LEFT JOIN public.analysis a  ON a.analysisid = sl.analysisid
                    LEFT JOIN public.sample   s  ON s.sampleid   = a.sampleid
                                                AND s.prefix     = a.prefix
                    LEFT JOIN ngam.ng3heingrowthdata ingr ON ingr.ingrowthid = sl.ingrowthid
                    WHERE res.runid = :rid
                    ORDER BY res.positioninrun
                """), {"rid": self._run_id}).fetchall()

            for r in rows:
                r = tuple(r)
                rejected = bool(r[17])
                vals = [
                    str(r[0] or ""),           # Pos
                    str(r[1] or ""),           # Lab ID
                    str(r[2] or ""),           # Sample Name
                    str(r[3] or ""),           # Type
                    str(r[4] or ""),           # Cycles
                    _sfmt(r[5],  4),           # BG (A)
                    _sfmt(r[6],  5),           # Net 3He (A)
                    _sfmt(r[7],  3),           # ± Unc
                    _sfmt(r[8],  4),           # Sensitivity
                    _sfmt(r[9],  5),           # Activity (ccSTP)
                    _sfmt(r[10], 3),           # ± Unc
                    _sfmt(r[11], 4),           # t_em (d)
                    _sfmt(r[12], 4),           # t_se (d)
                    _sfmt(r[13], 4),           # Water (g)
                    _sfmt(r[14], 4),           # Corr. Factor
                    _sfmt(r[15], 5),           # Act. Corr. (TU)
                    _sfmt(r[16], 3),           # ± Unc
                    "Yes" if rejected else "No",
                ]
                items = [QStandardItem(v) for v in vals]
                for it in items:
                    it.setEditable(False)
                if rejected:
                    for it in items:
                        it.setBackground(QBrush(QColor(255, 220, 220)))
                self.resultsModel.appendRow(items)

            h = self.tblResults.horizontalHeader()
            for i in range(CR.COUNT - 1):
                h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
            h.setSectionResizeMode(CR.COUNT - 1, QHeaderView.Stretch)

        except Exception as e:
            logging.error("Load sequence results failed: %s", e)

    # ── import MS data ────────────────────────────────────────────────────────

    def _open_import_dialog(self):
        """Open the Helix SFT import dialog for this run."""
        from ngam_ms_import_gui import NGAMImportDialog  # local import avoids circular deps

        # Fetch the current datapath from the DB
        current_datapath = ""
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text(
                    "SELECT datapath FROM ngam.msrun WHERE runid = :rid AND measurement_mode = 'IG'"
                ), {"rid": self._run_id}).fetchone()
                if row and row[0]:
                    current_datapath = str(row[0])
        except Exception as exc:
            logging.warning("Could not fetch datapath: %s", exc)

        dlg = NGAMImportDialog(
            run_id=self._run_id,
            datapath=current_datapath,
            parent=self,
        )
        if dlg.exec_() == NGAMImportDialog.Accepted:
            self._load_run()
            self._qc_widget.refresh()

    # ── QC / Validation tab ───────────────────────────────────────────────────

    def _open_qc_tab(self):
        """Switch to the QC / Validation tab and refresh the widget."""
        self._qc_widget.refresh()
        self._tabs.setCurrentIndex(2)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    if len(sys.argv) > 1:
        dlg = NGAMSequenceRunWindow(run_id=int(sys.argv[1]))
    else:
        dlg = NGAMSequenceRunWindow(run_id=1)
    dlg.show()
    sys.exit(app.exec_())
