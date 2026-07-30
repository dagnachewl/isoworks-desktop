"""
ngam_ng_sequence_run_gui.py
============================
NGAM – Noble Gas MS Sequence (non-ingrowth): Single Run Detail Window

Displays the header of an NGSequenceRun and its NGPreparations load list.

Schema (ngam.ngsequencerun):
    inoblesequenceid, runstatus (6=ready, 7=complete),
    workflowid, islocked, procedureid, equipmentid,
    flvtimestart, flvtimeend,
    createdatestamp, createuserstamp

Schema (ngam.ngpreparations):
    isequenceid, analysisid, iidinsequence (position),
    iportnumber, bisblank, bisreproreference, bislinreference,
    nvcReferenceGas, freferenceamount, nvcremarks, blocked
"""
from __future__ import annotations
import sys
import logging
import pathlib
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QLineEdit, QTextEdit, QDateTimeEdit,
    QPushButton, QTableView, QHeaderView,
    QAbstractItemView, QMessageBox, QCheckBox, QFrame,
    QDoubleSpinBox, QFileDialog, QGraphicsDropShadowEffect,
)
from PyQt5.QtCore import Qt, QDateTime
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QColor, QBrush

from db_core import db_manager
from sqlalchemy import text
from shared_utils import check_employee_privilege, normalize_login_name, get_current_user_id
from gui_utils import show_message
from ngam_script_generator import (
    NgInletDef, NgPipetteDef, NgVesselDef, NgScriptGenerator,
    DEFAULT_PIPETTES, DEFAULT_VESSELS,
)
from sample_label_gui import SampleLabelDialog, analysis_spec


# ─────────────────────────────── column constants ────────────────────────────
class C:
    POS        = 0
    AID        = 1
    PORT       = 2
    LABID      = 3
    SNAME      = 4
    BLANK      = 5
    REPRO      = 6
    LIN        = 7
    REFGAS     = 8
    REFAMT     = 9
    REMARKS    = 10

    HEADERS = [
        "Inlet", "AnalysisID", "Port",
        "Lab ID", "Sample Name",
        "Blank?", "Repro Ref?", "Lin Ref?",
        "Ref Gas", "Ref Amount",
        "Remarks",
    ]


# ─────────────────────────────── palette ─────────────────────────────────────
_HDR_BG   = "#1F3A5F"
_PANEL_BG = "#EAF0F6"

# Shared "glass" style for the header command buttons -- same look as the
# Import from Protocol screen: one uniform cool-blue translucent gradient
# instead of a distinct color per action, with a soft glow companion effect
# (QSS alone can't do box-shadow-style glows in Qt).
_BTN_GLASS = (
    "QPushButton {"
    "  background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
    "    stop:0 rgba(120, 190, 255, 190), stop:0.5 rgba(66, 165, 245, 205), stop:1 rgba(25, 118, 210, 215));"
    "  color: white; font-weight: 600;"
    "  border: 1px solid rgba(255, 255, 255, 130);"
    "  border-radius: 8px;"
    "  padding: 3px 14px;"
    "}"
    "QPushButton:hover {"
    "  background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
    "    stop:0 rgba(165, 217, 255, 220), stop:0.5 rgba(93, 188, 255, 230), stop:1 rgba(35, 140, 235, 235));"
    "  border: 1px solid rgba(255, 255, 255, 200);"
    "}"
    "QPushButton:pressed {"
    "  background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
    "    stop:0 rgba(20, 90, 170, 230), stop:1 rgba(10, 60, 130, 230));"
    "  border: 1px solid rgba(255, 255, 255, 90);"
    "}"
    "QPushButton:disabled {"
    "  background: rgba(176, 190, 197, 110);"
    "  color: rgba(255, 255, 255, 140);"
    "  border: 1px solid rgba(255, 255, 255, 60);"
    "}"
)


def _apply_glass_glow(btn: QPushButton) -> None:
    """Soft cool-blue glow behind a glass button -- companion to _BTN_GLASS."""
    glow = QGraphicsDropShadowEffect(btn)
    glow.setBlurRadius(18)
    glow.setOffset(0, 2)
    glow.setColor(QColor(66, 165, 245, 160))
    btn.setGraphicsEffect(glow)

# Row colours by type (mirrors the 3He sequence colour coding)
_COL_BLANK  = QColor(200, 230, 255)   # light blue
_COL_REPRO  = QColor(200, 255, 230)   # mint green
_COL_LIN    = QColor(255, 235, 190)   # light amber
_COL_SAMPLE = QColor(220, 255, 220)   # light green


class NGAMNGSequenceRunWindow(QDialog):
    """Read-only view of a single Noble Gas MS Sequence Run."""

    def __init__(self, run_id: int, parent=None):
        super().__init__(parent)
        self._run_id = run_id
        self._locked = False
        self.setWindowTitle(f"NG Sequence Run  #{run_id}")
        self.resize(1150, 700)
        self.setMinimumSize(900, 520)

        self._check_privileges()
        self._build_ui()
        self._load_run()

    def _check_privileges(self):
        try:
            user = normalize_login_name(get_current_user_id())
            self.has_write_priv = check_employee_privilege(user, "ngamaccess")
            self.has_admin_priv = check_employee_privilege(user, "ngamadmin")
        except Exception:
            self.has_write_priv = False
            self.has_admin_priv = False

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── header bar: title + status + action buttons (top-right) ──────────
        hdr_w = QFrame()
        hdr_w.setFixedHeight(44)
        hdr_w.setStyleSheet(f"background:{_HDR_BG};")
        hdr_lay = QHBoxLayout(hdr_w)
        hdr_lay.setContentsMargins(10, 4, 10, 4)
        hdr_lay.setSpacing(8)

        title_lbl = QLabel(f"  Noble Gas MS Sequence Run   #{self._run_id}")
        title_lbl.setStyleSheet(
            "color:white; font-weight:bold; font-size:13px; background:transparent;"
        )
        hdr_lay.addWidget(title_lbl)

        self.lblFooter = QLabel("")
        self.lblFooter.setStyleSheet(
            "color:#B0BEC5; background:transparent; font-size:11px;"
        )
        hdr_lay.addWidget(self.lblFooter, 1)

        def _hdr_btn(label):
            b = QPushButton(label)
            b.setFixedHeight(28)
            b.setStyleSheet(_BTN_GLASS)
            _apply_glass_glow(b)
            return b

        self.btnImportMS = _hdr_btn("Import MS Data")
        self.btnImportMS.clicked.connect(self._open_import_dialog)

        self.btnImportProtocol = _hdr_btn("Import from Protocol")
        self.btnImportProtocol.setToolTip(
            "Import signal data from a LabVIEW .protocol file into this run"
        )
        self.btnImportProtocol.clicked.connect(self._open_import_from_protocol)

        self.btnFinalize = _hdr_btn("Finalize Run")
        self.btnFinalize.clicked.connect(self._finalize_run)

        self.btnGenerateScripts = _hdr_btn("Generate Scripts")
        self.btnGenerateScripts.setToolTip(
            "Generate LabView control scripts and Qtegra load list for this run"
        )
        self.btnGenerateScripts.clicked.connect(self._generate_scripts)

        self.btnPrintLabels = _hdr_btn("Print Labels")
        self.btnPrintLabels.clicked.connect(self._print_ng_sequence_labels)

        btnClose = _hdr_btn("Close")
        btnClose.clicked.connect(self.reject)

        for btn in (self.btnImportMS, self.btnImportProtocol, self.btnFinalize,
                    self.btnGenerateScripts, self.btnPrintLabels, btnClose):
            hdr_lay.addWidget(btn)

        root.addWidget(hdr_w)

        body = QVBoxLayout()
        body.setContentsMargins(10, 8, 10, 8)
        body.setSpacing(8)
        root.addLayout(body, 1)

        body.addWidget(self._build_header_group())
        body.addWidget(self._build_preparations_group(), 1)

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

        def ro():
            w = QLineEdit()
            w.setReadOnly(True)
            w.setStyleSheet("background:white;")
            return w

        self.txtWorkflow  = ro()
        self.txtProcedure = ro()
        self.txtEquipment = ro()
        self.dtStart = QDateTimeEdit(); self.dtStart.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dtStart.setCalendarPopup(True); self.dtStart.setReadOnly(True)
        self.dtStart.setMinimumDateTime(QDateTime(1900, 1, 1, 0, 0))
        self.dtStart.setSpecialValueText("—"); self.dtStart.setStyleSheet("background:white;")
        self.dtEnd = QDateTimeEdit(); self.dtEnd.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dtEnd.setCalendarPopup(True); self.dtEnd.setReadOnly(True)
        self.dtEnd.setMinimumDateTime(QDateTime(1900, 1, 1, 0, 0))
        self.dtEnd.setSpecialValueText("—"); self.dtEnd.setStyleSheet("background:white;")
        self.txtStatus    = ro()
        self.chkLocked    = QCheckBox("Locked")
        self.chkLocked.setEnabled(False)

        for row, (label, widget) in enumerate([
            ("Workflow:",  self.txtWorkflow),
            ("Procedure:", self.txtProcedure),
            ("Device:",    self.txtEquipment),
        ]):
            lay.addWidget(lbl(label), row, 0)
            lay.addWidget(widget, row, 1)

        for row, (label, widget) in enumerate([
            ("Start Time:", self.dtStart),
            ("End Time:",   self.dtEnd),
            ("Status:",     self.txtStatus),
            ("Locked:",     self.chkLocked),
        ]):
            lay.addWidget(lbl(label), row, 2)
            lay.addWidget(widget, row, 3)

        lay.setColumnStretch(1, 1)
        lay.setColumnStretch(3, 1)
        return grp

    def _build_preparations_group(self) -> QGroupBox:
        grp = QGroupBox("Preparations (Load List)")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(6, 6, 6, 6)

        self.tblPrep = QTableView()
        self.tblPrep.setShowGrid(False)
        self.tblPrep.setAlternatingRowColors(False)  # we colour by type
        self.tblPrep.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblPrep.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblPrep.setSortingEnabled(True)
        self.tblPrep.verticalHeader().setVisible(False)
        self.tblPrep.setStyleSheet("""
            QTableView { border: none; background-color: white; }
            QTableView::item { padding: 4px 6px; }
            QTableView::item:selected { background-color: #DDEEFF; color: #000; }
            QHeaderView::section { background-color: #1F3A5F; color: white;
                                   font-weight: bold; padding: 4px 6px;
                                   border: 1px solid #0F2A4F; }
        """)
        self.prepModel = QStandardItemModel()
        self.prepModel.setHorizontalHeaderLabels(C.HEADERS)
        self.tblPrep.setModel(self.prepModel)
        lay.addWidget(self.tblPrep, 1)
        return grp

    # ── data loading ──────────────────────────────────────────────────────────

    def _load_run(self):
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT
                        r.runid, r.runstatus, r.islocked,
                        r.runstarttime,
                        r.runendtime,
                        w.workflowname,
                        ap.procedurename,
                        eq.equipmentname
                    FROM ngam.msrun r
                    LEFT JOIN public.workflow          w  ON w.workflowid  = r.workflowid
                    LEFT JOIN public.analysisprocedure ap ON ap.procedureid = r.procedureid
                    LEFT JOIN public.equipment         eq ON eq.equipmentid = r.equipmentid
                    WHERE r.runid = :rid AND r.measurement_mode = 'NG'
                """), {"rid": self._run_id}).fetchone()

            if not row:
                show_message(self, "Not Found",
                             f"NG Sequence Run {self._run_id} not found.", QMessageBox.Warning)
                return

            self._locked = bool(row[2])
            self.txtWorkflow.setText(row[5] or "")
            self.txtProcedure.setText(row[6] or "")
            self.txtEquipment.setText(row[7] or "")
            if row[3]:
                self.dtStart.setDateTime(QDateTime(row[3].year, row[3].month, row[3].day, row[3].hour, row[3].minute))
            else:
                self.dtStart.setDateTime(self.dtStart.minimumDateTime())
            if row[4]:
                self.dtEnd.setDateTime(QDateTime(row[4].year, row[4].month, row[4].day, row[4].hour, row[4].minute))
            else:
                self.dtEnd.setDateTime(self.dtEnd.minimumDateTime())
            _STAT = {6: "Ready (6)", 7: "Complete (7)"}
            self.txtStatus.setText(_STAT.get(row[1], str(row[1] or "")))
            self.chkLocked.setChecked(self._locked)

            is_complete = row[4] is not None
            self.btnFinalize.setEnabled(
                self.has_write_priv and not is_complete and not self._locked
            )
            self.setWindowTitle(
                f"NG Sequence Run  #{self._run_id}"
                + ("  [COMPLETE]" if is_complete else "  [Ready]")
            )

        except Exception as e:
            logging.error(f"Load NG sequence run {self._run_id} failed: {e}")
            show_message(self, "Error", str(e), QMessageBox.Critical)
            return

        self._load_preparations()

    def _load_preparations(self):
        self.prepModel.removeRows(0, self.prepModel.rowCount())
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT
                        p.positioninrun,
                        p.analysisid,
                        COALESCE(CAST(p.iportnumber AS TEXT), ''),
                        COALESCE(a.prefix || '-' || CAST(a.sampleid AS TEXT), ''),
                        COALESCE(s.sname, ''),
                        p.bisblank,
                        p.bisreproreference,
                        p.bislinreference,
                        COALESCE(p.nvcreferencegas, ''),
                        p.freferenceamount,
                        COALESCE(p.nvcremarks, ''),
                        p.ref_size,
                        p.ref_instance,
                        p.ref_size2,
                        p.ref_instance2
                    FROM ngam.ngpreparations p
                    LEFT JOIN public.analysis a ON a.analysisid = p.analysisid
                    LEFT JOIN public.sample   s ON s.sampleid = a.sampleid
                                              AND s.prefix    = a.prefix
                    WHERE p.runid = :rid
                    ORDER BY p.positioninrun
                """), {"rid": self._run_id}).fetchall()

            for r in rows:
                r = tuple(r)
                ref_gas, ref_size, ref_instance, ref_size2, ref_instance2 = r[8], r[11], r[12], r[13], r[14]
                # Standard/reference positions: "Standard_{gas}{size}{instance}"
                # (e.g. "Standard_SpikeLarge1") -- same fields/concatenation as
                # NgamSequence.tsx's prepRefDisplayName(); falls back to the
                # plain sample name for blanks/samples with no ref_size set.
                if ref_gas in ("Spike", "Air") and ref_size:
                    second = f"{ref_size2}{ref_instance2 if ref_instance2 is not None else ''}" if ref_size2 else ""
                    sample_name = f"Standard_{ref_gas}{ref_size}{ref_instance if ref_instance is not None else ''}{second}"
                else:
                    sample_name = r[4] or ""
                items = [
                    QStandardItem(str(r[0] or "")),   # Pos
                    QStandardItem(str(r[1] or "")),   # AID
                    QStandardItem(str(r[2])),          # Port
                    QStandardItem(str(r[3] or "")),   # LabID
                    QStandardItem(sample_name),        # sName
                    QStandardItem("Yes" if r[5] else "No"),  # Blank
                    QStandardItem("Yes" if r[6] else "No"),  # Repro
                    QStandardItem("Yes" if r[7] else "No"),  # Lin
                    QStandardItem(str(r[8])),          # RefGas
                    QStandardItem(                     # RefAmt
                        f"{r[9]:.4g}" if r[9] is not None else ""),
                    QStandardItem(str(r[10])),         # Remarks
                ]
                for it in items:
                    it.setEditable(False)
                # Colour by type
                if r[5]:
                    colour = _COL_BLANK
                elif r[6]:
                    colour = _COL_REPRO
                elif r[7]:
                    colour = _COL_LIN
                else:
                    colour = _COL_SAMPLE
                for it in items:
                    it.setBackground(QBrush(colour))
                self.prepModel.appendRow(items)

            h = self.tblPrep.horizontalHeader()
            for i in range(len(C.HEADERS) - 1):
                h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
            h.setSectionResizeMode(len(C.HEADERS) - 1, QHeaderView.Stretch)

            self.lblFooter.setText(
                f"{self.prepModel.rowCount()} preparation(s) in load list."
            )

        except Exception as e:
            logging.error(f"Load NG sequence preparations failed: {e}")
            show_message(self, "Error", str(e), QMessageBox.Critical)

    # ── import MS data ────────────────────────────────────────────────────────

    def _open_import_dialog(self):
        from ngam_ng_import_gui import NGAMNGImportDialog

        # Fetch current datapath from DB (graceful if column not yet present)
        datapath = ""
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT datapath
                    FROM ngam.msrun
                    WHERE runid = :rid AND measurement_mode = 'NG'
                """), {"rid": self._run_id}).fetchone()
                if row and row[0]:
                    datapath = str(row[0])
        except Exception:
            # Column may not exist yet — ignore
            pass

        dlg = NGAMNGImportDialog(self._run_id, datapath or "", parent=self)
        if dlg.exec_() == dlg.Accepted:
            self._load_run()

    def _open_import_from_protocol(self):
        from PyQt5.QtWidgets import QDialog, QVBoxLayout
        from PyQt5.QtCore import Qt
        from ngam_import_from_protocol_gui import ImportFromProtocolWidget

        dlg = QDialog(self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.setWindowTitle(f"Import from Protocol — Run #{self._run_id}")
        dlg.setWindowFlags(
            Qt.Window | Qt.WindowMinimizeButtonHint |
            Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint
        )

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        widget = ImportFromProtocolWidget(parent=dlg, target_run_id=self._run_id)
        layout.addWidget(widget, 1)

        def on_finished(_):
            try:
                widget.model.reset()
                if hasattr(widget, "_results_widget") and hasattr(widget._results_widget, "clear"):
                    widget._results_widget.clear()
                widget.reset_ui_state()
            except Exception:
                pass
            self._load_run()

        dlg.finished.connect(on_finished)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.showMaximized()   # window-modal + maximized; independent popups stay accessible

    # ── finalize ──────────────────────────────────────────────────────────────
    def _print_ng_sequence_labels(self):
        """Fetch samples for this noble gas sequence run and open the QR label printer."""
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT p.positioninrun,
                           a.prefix, a.sampleid, s.sname,
                           p.analysisid,
                           p.bisblank, p.bisreproreference, p.bislinreference,
                           p.nvcreferencegas, p.ref_size, p.ref_instance,
                           p.ref_size2, p.ref_instance2
                    FROM   ngam.ngpreparations p
                    LEFT JOIN public.analysis a ON a.analysisid = p.analysisid
                    LEFT JOIN public.sample   s ON s.sampleid   = a.sampleid
                                             AND s.prefix     = a.prefix
                    WHERE  p.runid = :r
                    ORDER  BY p.positioninrun
                """), {"r": self._run_id}).fetchall()
        except Exception as exc:
            logging.error("Sequence label fetch: %s", exc)
            show_message(self, "Error", str(exc))
            return
        if not rows:
            show_message(self, "No Data", "No samples found for this run.")
            return
        specs = []
        for r in rows:
            ref_gas, ref_size, ref_instance, ref_size2, ref_instance2 = r[8], r[9], r[10], r[11], r[12]
            if ref_gas in ("Spike", "Air") and ref_size:
                second = f"{ref_size2}{ref_instance2 if ref_instance2 is not None else ''}" if ref_size2 else ""
                sample_name = f"Standard_{ref_gas}{ref_size}{ref_instance if ref_instance is not None else ''}{second}"
            elif r[5]:
                sample_name = "Blank"
            else:
                sample_name = r[3] or ""

            prefix = r[1] or ""
            sample_id = str(r[2]) if r[2] is not None else ""
            analysis_id = str(r[4]) if r[4] is not None else ""

            specs.append(
                analysis_spec(
                    prefix=prefix,
                    sample_id=sample_id,
                    analysis_id=analysis_id,
                    container_num=f"Port {r[0]}",
                    job_type="NGSeq",
                    sample_name=sample_name,
                )
            )
        SampleLabelDialog.show_batch(specs, parent=self).exec_()

    def _finalize_run(self):
        reply = QMessageBox.question(
            self, "Finalize Run",
            f"Mark NG Sequence Run {self._run_id} as complete?\n\n"
            "This sets fLVTimeEnd to now and RunStatus to 7.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            now = datetime.now()
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE ngam.msrun
                    SET runendtime = :now, runstatus = 7
                    WHERE runid = :rid AND measurement_mode = 'NG'
                """), {"now": now, "rid": self._run_id})
                conn.commit()
            self._load_run()
        except Exception as e:
            logging.error(f"Finalize NG sequence run failed: {e}")
            show_message(self, "Error", str(e), QMessageBox.Critical)


    # ── generate LabView scripts ──────────────────────────────────────────────

    def _load_pipettes_from_db(self, conn) -> list[NgPipetteDef] | None:
        rows = conn.execute(text("""
            SELECT pipette_name, control_type, valve_in, valve_out, select_switch,
                   volume, vessel_name, initial_counter, actual_counter
            FROM ngam.ng_pipette
            WHERE is_active = 1
            ORDER BY id
        """)).fetchall()
        if not rows:
            return None
        return [NgPipetteDef(*tuple(r)) for r in rows]

    def _load_vessels_from_db(self, conn) -> list[NgVesselDef] | None:
        rows = conn.execute(text("""
            SELECT vessel_name, gas_name, volume, tubing_volume,
                   fill_pressure, fill_temperature, fill_humidity,
                   is_live_conditions
            FROM ngam.ng_reference_vessel
            WHERE is_active = 1
            ORDER BY id
        """)).fetchall()
        if not rows:
            return None
        return [
            NgVesselDef(
                name=r[0], gas_name=r[1], volume=r[2], tubing_volume=r[3],
                fill_pressure=r[4], fill_temperature=r[5], fill_humidity=r[6],
                is_live_conditions=bool(r[7]),
            )
            for r in rows
        ]

    def _load_inlets_for_scripts(self, conn) -> list[NgInletDef]:
        rows = conn.execute(text("""
            SELECT
                p.positioninrun,
                p.analysisid,
                p.iportnumber,
                COALESCE(a.prefix || '-' || CAST(a.sampleid AS TEXT), '') AS lab_id,
                COALESCE(s.sname, '')                                      AS sname,
                COALESCE(p.bisblank, FALSE),
                COALESCE(p.bisreproreference, FALSE),
                COALESCE(p.bislinreference, FALSE),
                COALESCE(p.nvcreferencegas, ''),
                p.freferenceamount
            FROM ngam.ngpreparations p
            LEFT JOIN public.analysis a ON a.analysisid = p.analysisid
            LEFT JOIN public.sample   s ON s.sampleid = a.sampleid
                                      AND s.prefix    = a.prefix
            WHERE p.runid = :rid
            ORDER BY p.positioninrun
        """), {"rid": self._run_id}).fetchall()
        return [
            NgInletDef(
                position=r[0], analysis_id=r[1], port_number=r[2],
                lab_id=r[3], sample_name=r[4],
                is_blank=bool(r[5]), is_repro_ref=bool(r[6]), is_lin_ref=bool(r[7]),
                ref_gas=r[8], ref_amount=r[9],
            )
            for r in rows
        ]

    def _generate_scripts(self):
        dlg = _ScriptGenDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            return

        output_folder = dlg.folder
        temperature   = dlg.temperature
        pressure      = dlg.pressure
        humidity      = dlg.humidity

        try:
            with db_manager.get_connection() as conn:
                inlets  = self._load_inlets_for_scripts(conn)
                pipettes = self._load_pipettes_from_db(conn)
                vessels  = self._load_vessels_from_db(conn)
        except Exception as e:
            logging.error(f"Generate scripts — DB query failed: {e}")
            show_message(self, "Error", f"Failed to load run data:\n{e}", QMessageBox.Critical)
            return

        if not inlets:
            show_message(self, "No Data",
                         "No preparations found for this run.", QMessageBox.Warning)
            return

        gen = NgScriptGenerator(
            run_id=self._run_id,
            inlets=inlets,
            protocol_folder=output_folder,
            valve_type="E",
            pipettes=pipettes,
            vessels=vessels,
        )
        files = gen.generate_all(temperature=temperature, pressure=pressure, humidity=humidity)

        out = pathlib.Path(output_folder)
        out.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        try:
            for filename, content in files.items():
                (out / filename).write_text(content, encoding="utf-8")
                written.append(filename)
        except Exception as e:
            logging.error(f"Generate scripts — write failed: {e}")
            show_message(self, "Error", f"Failed to write files:\n{e}", QMessageBox.Critical)
            return

        show_message(
            self, "Scripts Generated",
            f"Created {len(written)} file(s) in:\n{output_folder}\n\n"
            + "\n".join(f"  • {fn}" for fn in sorted(written)),
        )


# ── Script generation dialog ──────────────────────────────────────────────────

class _ScriptGenDialog(QDialog):
    """Collects output folder + air conditions before generating scripts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Generate LabView Scripts")
        self.setFixedWidth(480)
        self.setModal(True)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        # Output folder
        grp_folder = QGroupBox("Output Folder")
        fl = QHBoxLayout(grp_folder)
        self.edtFolder = QLineEdit()
        self.edtFolder.setPlaceholderText(
            r"e.g. D:\NobleControl\Scripts\Helix_Ver1\SequenceDetails"
        )
        btn_browse = QPushButton("Browse…")
        btn_browse.setFixedWidth(80)
        btn_browse.clicked.connect(self._browse)
        fl.addWidget(self.edtFolder, 1)
        fl.addWidget(btn_browse)
        lay.addWidget(grp_folder)

        # Air conditions
        grp_air = QGroupBox("Air Conditions (for PipetteSettings.txt)")
        gl = QGridLayout(grp_air)
        gl.setColumnStretch(1, 1)

        def _spin(lo, hi, dec, val, suffix):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setDecimals(dec)
            s.setValue(val)
            s.setSuffix(suffix)
            return s

        self.spnTemp     = _spin(-50, 60,   1, 20.0,    " °C")
        self.spnPressure = _spin(800, 1100, 1, 1013.25, " hPa")
        self.spnHumidity = _spin(0,  100,   1, 50.0,    " %")

        for row, (label, widget) in enumerate([
            ("Temperature:",    self.spnTemp),
            ("Pressure:",       self.spnPressure),
            ("Rel. Humidity:",  self.spnHumidity),
        ]):
            lbl = QLabel(label)
            lbl.setStyleSheet("font-weight:bold;")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            gl.addWidget(lbl, row, 0)
            gl.addWidget(widget, row, 1)

        lay.addWidget(grp_air)

        # Buttons
        btn_lay = QHBoxLayout()
        btn_ok = QPushButton("Generate")
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self._accept)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_lay.addStretch()
        btn_lay.addWidget(btn_ok)
        btn_lay.addWidget(btn_cancel)
        lay.addLayout(btn_lay)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Output Folder", self.edtFolder.text() or ""
        )
        if folder:
            self.edtFolder.setText(folder)

    def _accept(self):
        if not self.edtFolder.text().strip():
            show_message(self, "Folder Required", "Please select an output folder.")
            return
        self.accept()

    @property
    def folder(self) -> str:
        return self.edtFolder.text().strip()

    @property
    def temperature(self) -> float:
        return self.spnTemp.value()

    @property
    def pressure(self) -> float:
        return self.spnPressure.value()

    @property
    def humidity(self) -> float:
        return self.spnHumidity.value()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    if len(sys.argv) > 1:
        dlg = NGAMNGSequenceRunWindow(run_id=int(sys.argv[1]))
    else:
        dlg = NGAMNGSequenceRunWindow(run_id=1)
    dlg.show()
    sys.exit(app.exec_())
