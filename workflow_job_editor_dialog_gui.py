"""
workflow_job_editor_dialog_gui.py — Workflow job editor dialog for IsoWorks.
Provides WorkflowJobEditorDialog (QDialog) for creating or editing a single
WorkflowJob record: job name (combo), procedure, assigned staff,
pre-requisite flag, and obsolete flag.
"""
import logging
from datetime import datetime
import getpass

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QComboBox, QCheckBox, QFormLayout,
    QDialogButtonBox, QSpinBox, QMessageBox
)

from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message


class WorkflowJobEditorDialog(QDialog):
    """
    Create or edit a single WorkflowJob record.
    Job name drives the procedure combo filter via Job_Procedure.CategoryID.
    """
    def __init__(self, workflow_id, workflow_job_id=None, parent=None):
        super().__init__(parent)
        self.wid = workflow_id
        self.jid = workflow_job_id
        self.is_new = (self.jid is None)

        self.setWindowTitle("New Job" if self.is_new else "Edit Job")
        self.setModal(True)
        self.setMinimumWidth(500)

        l = QVBoxLayout(self)
        f = QFormLayout()

        self.spnSeq      = QSpinBox();  self.spnSeq.setRange(1, 999)
        self.cmbJobName  = QComboBox()
        self.cmbProc     = QComboBox()
        self.cmbEmployee = QComboBox()
        self.chkPreReq   = QCheckBox("Is Pre-Requisite")
        self.chkObs      = QCheckBox("Obsolete")

        f.addRow("Sequence:",  self.spnSeq)
        f.addRow("Job Name:",  self.cmbJobName)
        f.addRow("Procedure:", self.cmbProc)
        f.addRow("Staff:",     self.cmbEmployee)
        f.addRow(self.chkPreReq)
        f.addRow(self.chkObs)
        l.addLayout(f)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        l.addWidget(bb)

        self.cmbJobName.currentIndexChanged.connect(self._on_job_name_changed)

        self._load_static_combos()
        self._load_data()

    # ── Static combo loaders ───────────────────────────────────────────────────

    def _load_static_combos(self):
        try:
            with db_manager.get_connection() as conn:
                self.cmbJobName.addItem("- Select -", None)
                _jn = db_manager.sql_concat('ID', "': '", 'sName')
                sql = f"SELECT ID, {_jn} FROM Job_Procedure ORDER BY sName"
                for r in conn.execute(text(sql)):
                    self.cmbJobName.addItem(r[1], r[0])
        except Exception as e:
            logging.warning("Failed to load job-name combo: %s", e)

        try:
            with db_manager.get_connection() as conn:
                self.cmbEmployee.addItem("- None -", None)
                _en = db_manager.sql_concat('LastName', "', '", 'FirstMiddleName')
                sql = f"SELECT EmployeeID, {_en} FROM Employee WHERE IsObsolete IS NOT TRUE ORDER BY LastName"
                for r in conn.execute(text(sql)):
                    self.cmbEmployee.addItem(r[1], r[0])
        except Exception as e:
            logging.warning("Failed to load employee combo: %s", e)

    def _reload_proc(self, cat_id, proc_id=None):
        """Reload Procedure combo filtered by job category."""
        self.cmbProc.blockSignals(True)
        self.cmbProc.clear()
        self.cmbProc.addItem("- None -", None)

        if cat_id is not None:
            try:
                with db_manager.get_connection() as conn:
                    _pn = db_manager.sql_concat('ProcedureID', "'--'", 'ProcedureName')
                    sql = (
                        f"SELECT ProcedureID, {_pn} "
                        f"FROM AnalysisProcedure WHERE CategoryID=:cid AND IsObsolete IS NOT TRUE "
                        f"ORDER BY ProcedureID"
                    )
                    for r in conn.execute(text(sql), {"cid": cat_id}):
                        self.cmbProc.addItem(r[1], r[0])
            except Exception as e:
                logging.warning("Failed to reload procedure combo: %s", e)

        if proc_id is not None:
            idx = self.cmbProc.findData(proc_id)
            if idx >= 0:
                self.cmbProc.setCurrentIndex(idx)

        self.cmbProc.blockSignals(False)

    def _on_job_name_changed(self):
        self._reload_proc(self.cmbJobName.currentData())

    # ── Data load / save ──────────────────────────────────────────────────────

    def _load_data(self):
        if self.is_new:
            try:
                with db_manager.get_connection() as conn:
                    res = conn.execute(
                        text("SELECT MAX(RunSequence) FROM WorkflowJob WHERE WorkflowID=:wid"),
                        {"wid": self.wid}
                    ).fetchone()
                    self.spnSeq.setValue((res[0] or 0) + 1)
            except Exception:
                self.spnSeq.setValue(1)
            return

        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(
                    text("SELECT * FROM WorkflowJob WHERE WorkflowJobID=:jid"),
                    {"jid": self.jid}
                ).fetchone()
                if not row:
                    return

                self.spnSeq.setValue(row.RunSequence or 1)

                cat_row = conn.execute(
                    text("SELECT ID FROM Job_Procedure WHERE sName=:nm"),
                    {"nm": row.JobName}
                ).fetchone()
                if cat_row:
                    self.cmbJobName.blockSignals(True)
                    idx = self.cmbJobName.findData(cat_row[0])
                    if idx >= 0:
                        self.cmbJobName.setCurrentIndex(idx)
                    self.cmbJobName.blockSignals(False)
                    self._reload_proc(cat_row[0], row.ProcedureID)

                emp_id = getattr(row, "EmployeeID", None)
                if emp_id is not None:
                    idx = self.cmbEmployee.findData(emp_id)
                    if idx >= 0:
                        self.cmbEmployee.setCurrentIndex(idx)

                self.chkPreReq.setChecked(bool(getattr(row, "IsPreRequisite", False)))
                self.chkObs.setChecked(bool(row.IsObsolete))

        except Exception as e:
            show_message(self, "Error", f"Failed to load job data: {e}", QMessageBox.Warning)

    def save(self):
        cat_id = self.cmbJobName.currentData()
        if cat_id is None:
            show_message(self, "Validation Error", "Please select a Job Name.", QMessageBox.Warning)
            return

        try:
            with db_manager.get_connection() as conn:
                name_row = conn.execute(
                    text("SELECT sName FROM Job_Procedure WHERE ID=:id"), {"id": cat_id}
                ).fetchone()
                job_name = name_row[0] if name_row else ""

                p = {
                    "wid":    self.wid,
                    "seq":    self.spnSeq.value(),
                    "nam":    job_name,
                    "pid":    self.cmbProc.currentData(),
                    "eid":    self.cmbEmployee.currentData(),
                    "prereq": self.chkPreReq.isChecked(),
                    "obs":    self.chkObs.isChecked(),
                    "now":    datetime.now(),
                    "usr":    getpass.getuser(),
                }

                if self.is_new:
                    sql = """
                        INSERT INTO WorkflowJob (
                            WorkflowID, RunSequence, JobName, ProcedureID,
                            EmployeeID, IsPreRequisite, IsObsolete,
                            ModifDateStamp, ModifUserStamp, CreateDateStamp, CreateUserStamp
                        ) VALUES (
                            :wid, :seq, :nam, :pid,
                            :eid, :prereq, :obs,
                            :now, :usr, :now, :usr
                        )
                    """
                else:
                    p["jid"] = self.jid
                    sql = """
                        UPDATE WorkflowJob SET
                            WorkflowID=:wid, RunSequence=:seq, JobName=:nam,
                            ProcedureID=:pid, EmployeeID=:eid,
                            IsPreRequisite=:prereq, IsObsolete=:obs,
                            ModifDateStamp=:now, ModifUserStamp=:usr
                        WHERE WorkflowJobID=:jid
                    """

                conn.execute(text(sql), p)
                conn.commit()
                self.accept()

        except Exception as e:
            logging.error("Failed to save workflow job: %s", e, exc_info=True)
            show_message(self, "Database Error", f"Failed to save record:\n{e}", QMessageBox.Critical)
