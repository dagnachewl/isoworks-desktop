"""
siam_preanalysis_create_gui.py — Pre-isotope analysis batch creation dialog for IsoWorks.
Provides PreAnalysisCreateDialog (QDialog) for creating new chemistry/pre-analysis
batches by selecting a workflow, instrument, and samples for the load list.
"""
from __future__ import annotations
import logging
import getpass
from datetime import datetime

from PyQt5.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QComboBox, QLineEdit, QPushButton, QTableView, QCheckBox, QLabel,
    QMessageBox, QHeaderView, QAbstractItemView
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QFont, QBrush, QColor
from PyQt5.QtCore import Qt

from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message


class PreAnalysisCreateDialog(QDialog):
    """
    Create a new Pre-Isotope Analysis (Chemistry) batch.
    Maps to VBA frmSIPreAnalysisCreate / ChemistryBatch + ChemistryLoadlist.

    Workflow:
      1. Select Workflow  →  auto-fills Procedure from WorkflowJob (JobName='Chemistry')
      2. Select Instrument (Equipment.CategoryID=6 or matching procedure format)
      3. Add available samples to the load list
      4. Click 'Create Batch'
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create Pre-Analysis Batch")
        self.resize(1100, 680)

        self.created_run_id: int | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Setup bar ─────────────────────────────────────────────────────
        setup_grp = QGroupBox("Batch Setup")
        setup_lay = QFormLayout(setup_grp)

        self.cmbWorkflow  = QComboBox()
        self.cmbProcedure = QComboBox()
        self.cmbDevice    = QComboBox()
        self.lblRequired  = QLabel("—")   # NumberOfSamples from procedure

        setup_lay.addRow("Workflow:",   self.cmbWorkflow)
        setup_lay.addRow("Procedure:",  self.cmbProcedure)
        setup_lay.addRow("Instrument:", self.cmbDevice)
        setup_lay.addRow("Positions:",  self.lblRequired)
        root.addWidget(setup_grp)

        # ── Three-panel layout: Available | arrows | Load list ─────────────
        content = QHBoxLayout()

        # Left: available samples
        av_grp  = QGroupBox("Available Samples")
        av_lay  = QVBoxLayout(av_grp)
        flt_row = QHBoxLayout()
        self.cmbStatus  = QComboBox()
        self.chkAllStat = QCheckBox("All")
        flt_row.addWidget(QLabel("Status:")); flt_row.addWidget(self.cmbStatus, 1)
        flt_row.addWidget(self.chkAllStat)
        av_lay.addLayout(flt_row)
        self.av_model = QStandardItemModel()
        self.av_table = QTableView()
        self.av_table.setModel(self.av_model)
        self.av_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.av_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.av_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.av_table.setSortingEnabled(True)
        self.av_table.setAlternatingRowColors(True)
        self.av_table.setShowGrid(False)
        av_lay.addWidget(self.av_table)
        content.addWidget(av_grp, 3)

        # Middle: transfer buttons
        mid = QVBoxLayout()
        mid.addStretch(1)
        for attr, lbl in [("btnAdd", ">"), ("btnAddAll", ">>"), ("btnRem", "<"), ("btnRemAll", "<<")]:
            b = QPushButton(lbl); b.setFixedWidth(36)
            setattr(self, attr, b); mid.addWidget(b)
        mid.addStretch(1)
        content.addLayout(mid)

        # Right: load list
        ll_grp = QGroupBox("Batch Load List")
        ll_lay = QVBoxLayout(ll_grp)
        sum_row = QHBoxLayout()
        self.lblTotal = QLabel("Total: 0"); self.lblTotal.setStyleSheet("font-weight:bold;")
        self.lblUnk   = QLabel("Unknowns: 0")
        sum_row.addWidget(self.lblTotal); sum_row.addSpacing(10); sum_row.addWidget(self.lblUnk); sum_row.addStretch()
        ll_lay.addLayout(sum_row)

        tbl_row = QHBoxLayout()
        self.ll_model = QStandardItemModel()
        self.ll_table = QTableView()
        self.ll_table.setModel(self.ll_model)
        self.ll_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ll_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.ll_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.ll_table.setAlternatingRowColors(True)
        self.ll_table.setShowGrid(False)
        tbl_row.addWidget(self.ll_table)

        v_tool = QVBoxLayout()
        self.btnUp   = QPushButton("▲"); self.btnUp.setFixedSize(24, 24)
        self.btnDown = QPushButton("▼"); self.btnDown.setFixedSize(24, 24)
        self.btnDel  = QPushButton("✖"); self.btnDel.setFixedSize(24, 24)
        self.btnDel.setStyleSheet("color:red;font-weight:bold;")
        v_tool.addStretch()
        v_tool.addWidget(self.btnUp); v_tool.addWidget(self.btnDown)
        v_tool.addSpacing(10); v_tool.addWidget(self.btnDel)
        v_tool.addStretch()
        tbl_row.addLayout(v_tool)
        ll_lay.addLayout(tbl_row)
        content.addWidget(ll_grp, 4)

        root.addLayout(content, 1)

        # ── Bottom buttons ────────────────────────────────────────────────
        bot = QHBoxLayout()
        bot.addStretch(1)
        self.btnCreate = QPushButton("Create Batch")
        self.btnCreate.setStyleSheet("background-color:#27ae60;color:white;font-weight:bold;")
        self.btnCancel = QPushButton("Cancel")
        bot.addWidget(self.btnCreate); bot.addWidget(self.btnCancel)
        root.addLayout(bot)

        # ── Signals ───────────────────────────────────────────────────────
        self.cmbWorkflow.currentIndexChanged.connect(self._on_workflow_changed)
        self.cmbProcedure.currentIndexChanged.connect(self._on_procedure_changed)
        self.cmbStatus.currentIndexChanged.connect(self._load_avail_samples)
        self.chkAllStat.toggled.connect(self._load_avail_samples)
        self.btnAdd.clicked.connect(self._add_selected)
        self.btnAddAll.clicked.connect(self._add_all)
        self.btnRem.clicked.connect(self._remove_selected)
        self.btnRemAll.clicked.connect(self._remove_all)
        self.btnUp.clicked.connect(self._move_up)
        self.btnDown.clicked.connect(self._move_down)
        self.btnDel.clicked.connect(self._delete_selected)
        self.btnCreate.clicked.connect(self._on_create)
        self.btnCancel.clicked.connect(self.reject)

        self._init_combos()

    # ──────────────────────────────────────────────────────────────────────
    # Initialisation
    # ──────────────────────────────────────────────────────────────────────

    def _init_combos(self):
        try:
            with db_manager.get_connection() as conn:
                # Workflows that have a 'Hydrochemistry' WorkflowJob
                self.cmbWorkflow.addItem("- Select -", None)
                rows = conn.execute(text("""
                    SELECT DISTINCT w.workflowid, w.workflowname
                    FROM public.workflow w
                    JOIN public.workflowjob wj ON w.workflowid = wj.workflowid
                    WHERE wj.jobname = 'Hydrochemistry'
                    ORDER BY w.workflowname
                """)).fetchall()
                for r in rows:
                    self.cmbWorkflow.addItem(f"{r[0]} — {r[1]}", r[0])

                # Status options
                self.cmbStatus.addItem("- All Pending -", None)
                for r in conn.execute(text(
                    "SELECT status, description FROM public.statuslookup WHERE status IN (222,1,3,4,5) ORDER BY status DESC"
                )):
                    self.cmbStatus.addItem(f"{r[0]} - {r[1]}", r[0])
                self.cmbStatus.setCurrentIndex(1)

        except Exception as exc:
            logging.error("PreAnalysisCreate._init_combos: %s", exc)

    def _on_workflow_changed(self):
        wid = self.cmbWorkflow.currentData()
        self.cmbProcedure.clear(); self.cmbDevice.clear()
        if not wid:
            self._load_avail_samples()
            return
        try:
            with db_manager.get_connection() as conn:
                # Procedure linked via WorkflowJob.JobName='Hydrochemistry'
                rows = conn.execute(text("""
                    SELECT p.procedureid, p.procedurename, p.numberofsamples
                    FROM public.analysisprocedure p
                    JOIN public.workflowjob wj ON wj.procedureid = p.procedureid
                    WHERE wj.workflowid = :wid AND wj.jobname = 'Hydrochemistry'
                    ORDER BY p.procedurename
                """), {"wid": wid}).fetchall()

                self.cmbProcedure.addItem("- Select -", None)
                for r in rows:
                    self.cmbProcedure.addItem(f"{r[0]} — {r[1]}", (r[0], r[2]))
                if len(rows) == 1:
                    self.cmbProcedure.setCurrentIndex(1)

        except Exception as exc:
            logging.error("PreAnalysisCreate._on_workflow_changed: %s", exc)

        self._load_avail_samples()

    def _on_procedure_changed(self):
        data = self.cmbProcedure.currentData()
        self.cmbDevice.clear()
        if not data:
            self.lblRequired.setText("—")
            return

        pid, n_samp = data
        self.lblRequired.setText(str(n_samp) if n_samp else "—")

        try:
            with db_manager.get_connection() as conn:
                # Hydrochemistry instruments (categoryid=4 per job_procedure)
                rows = conn.execute(text("""
                    SELECT e.equipmentid, e.equipmentname
                    FROM public.equipment e
                    WHERE e.categoryid = 4 AND e.isobsolete IS NOT TRUE
                    ORDER BY e.equipmentname
                """)).fetchall()
                for r in rows:
                    self.cmbDevice.addItem(r[1], r[0])
        except Exception as exc:
            logging.error("PreAnalysisCreate._on_procedure_changed: %s", exc)

        # Re-init load list template (sequential blank positions)
        self._init_load_list()

    def _init_load_list(self):
        """Pre-populate the load list with blank sequential positions from procedure."""
        self.ll_model.clear()
        self.ll_model.setHorizontalHeaderLabels(
            ["Pos", "Prefix", "Sample ID", "Sample Name", "Status"]
        )
        data = self.cmbProcedure.currentData()
        if not data:
            return
        _pid, n_samp = data
        n = n_samp or 0
        for i in range(n):
            items = [
                QStandardItem(str(i + 1)),
                QStandardItem(""), QStandardItem(""), QStandardItem(""),
                QStandardItem("Pending"),
            ]
            items[0].setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.ll_model.appendRow(items)
        self._update_summary()

    # ──────────────────────────────────────────────────────────────────────
    # Available samples
    # ──────────────────────────────────────────────────────────────────────

    def _load_avail_samples(self):
        self.av_model.clear()
        self.av_model.setHorizontalHeaderLabels(["OurLabID", "Name", "Status", "SubmissionID"])

        wid = self.cmbWorkflow.currentData()
        if not wid:
            return

        # Build exclusion set from current load list
        excluded = set()
        for r in range(self.ll_model.rowCount()):
            sid = self.ll_model.item(r, 2).text()
            pfx = self.ll_model.item(r, 1).text()
            if sid and pfx:
                excluded.add((pfx, int(sid)))

        show_all = self.chkAllStat.isChecked()
        stat_val = self.cmbStatus.currentData()

        try:
            with db_manager.get_connection() as conn:
                sql = f"""
                    SELECT s.sampleid, s.prefix, s.sname, s.status,
                           {db_manager.sql_concat("s.prefix", "'-'", "s.sampleid")},
                           sub.submissionid
                    FROM public.sample s
                    JOIN public.submission sub ON sub.submissionid = s.submissionid
                    JOIN public.sample_queue sq ON sq.sampleid = s.sampleid AND sq.prefix = s.prefix
                    JOIN public.workflowjob wj ON wj.workflowjobid = sq.workflowjobid
                    WHERE wj.workflowid = :wid AND s.sampletype IN (0,3,6)
                """
                params: dict = {"wid": wid}

                if not show_all and stat_val is not None:
                    sql += " AND s.status = :stat"
                    params["stat"] = stat_val

                sql += " ORDER BY sub.submissiondate, s.sampleid"
                rows = conn.execute(text(sql), params).fetchall()

            for row in rows:
                sid, pfx, name, stat, lab_id, sub_id = row
                if (pfx, sid) in excluded:
                    continue
                item = QStandardItem(lab_id)
                item.setData(sid,  Qt.UserRole + 1)
                item.setData(pfx,  Qt.UserRole + 2)
                item.setData(name, Qt.UserRole + 3)
                self.av_model.appendRow([
                    item,
                    QStandardItem(name or ""),
                    QStandardItem(str(stat)),
                    QStandardItem(str(sub_id)),
                ])

            self.av_table.resizeColumnsToContents()

        except Exception as exc:
            logging.error("PreAnalysisCreate._load_avail_samples: %s", exc)

    # ──────────────────────────────────────────────────────────────────────
    # Transfer buttons
    # ──────────────────────────────────────────────────────────────────────

    def _first_empty_slot(self) -> int:
        for r in range(self.ll_model.rowCount()):
            if not self.ll_model.item(r, 2).text():
                return r
        return -1

    def _add_selected(self):
        sel = self.av_table.selectionModel().selectedRows()
        to_remove = []
        for idx in sorted(sel, key=lambda x: x.row()):
            slot = self._first_empty_slot()
            if slot == -1:
                break
            row = idx.row()
            item = self.av_model.item(row, 0)
            sid  = item.data(Qt.UserRole + 1)
            pfx  = item.data(Qt.UserRole + 2)
            name = item.data(Qt.UserRole + 3)
            self.ll_model.item(slot, 1).setText(pfx or "")
            self.ll_model.item(slot, 2).setText(str(sid) if sid is not None else "")
            self.ll_model.item(slot, 3).setText(name or "")
            to_remove.append(row)
        for r in sorted(to_remove, reverse=True):
            self.av_model.removeRow(r)
        self._update_summary()

    def _add_all(self):
        self.av_table.selectAll()
        self._add_selected()

    def _remove_selected(self):
        for idx in self.ll_table.selectionModel().selectedRows():
            r = idx.row()
            for c in [1, 2, 3]:
                self.ll_model.item(r, c).setText("")
            self.ll_model.item(r, 4).setText("Pending")
        self._load_avail_samples()
        self._update_summary()

    def _remove_all(self):
        self.ll_table.selectAll()
        self._remove_selected()

    def _move_up(self):
        sel = self.ll_table.selectionModel().selectedRows()
        if not sel:
            return
        row = sel[0].row()
        if row > 0:
            self.ll_model.insertRow(row - 1, self.ll_model.takeRow(row))
            self.ll_table.selectRow(row - 1)
            self._renumber()

    def _move_down(self):
        sel = self.ll_table.selectionModel().selectedRows()
        if not sel:
            return
        row = sel[0].row()
        if row < self.ll_model.rowCount() - 1:
            self.ll_model.insertRow(row + 1, self.ll_model.takeRow(row))
            self.ll_table.selectRow(row + 1)
            self._renumber()

    def _delete_selected(self):
        sel = self.ll_table.selectionModel().selectedRows()
        for idx in sorted(sel, key=lambda x: x.row(), reverse=True):
            self.ll_model.removeRow(idx.row())
        self._renumber()
        self._update_summary()

    def _renumber(self):
        for r in range(self.ll_model.rowCount()):
            self.ll_model.item(r, 0).setText(str(r + 1))

    def _update_summary(self):
        total = self.ll_model.rowCount()
        filled = sum(1 for r in range(total) if self.ll_model.item(r, 2).text())
        self.lblTotal.setText(f"Total: {total}")
        self.lblUnk.setText(f"Filled: {filled}")

    # ──────────────────────────────────────────────────────────────────────
    # Create batch
    # ──────────────────────────────────────────────────────────────────────

    def _on_create(self):
        wid  = self.cmbWorkflow.currentData()
        proc_data = self.cmbProcedure.currentData()
        eid  = self.cmbDevice.currentData()

        if not wid or not proc_data:
            show_message(self, "Missing", "Please select a Workflow and Procedure.", QMessageBox.Warning)
            return
        if not eid:
            show_message(self, "Missing", "Please select an Instrument.", QMessageBox.Warning)
            return

        pid = proc_data[0]

        # Collect filled rows
        samples = []
        for r in range(self.ll_model.rowCount()):
            pfx = self.ll_model.item(r, 1).text()
            sid_str = self.ll_model.item(r, 2).text()
            if pfx and sid_str:
                samples.append((int(self.ll_model.item(r, 0).text()), pfx, int(sid_str)))

        if not samples:
            show_message(self, "Empty", "No samples added to the batch.", QMessageBox.Warning)
            return

        try:
            with db_manager.get_connection() as conn:
                now = datetime.now()
                user = getpass.getuser()

                # Repeats
                res = conn.execute(text(
                    "SELECT MAX(repeats) FROM public.analysisprocedure_measurable WHERE procedureid = :pid"
                ), {"pid": pid}).fetchone()
                repeats = max(res[0], 1) if res and res[0] is not None else 1

                # Resolve workflowjobid for chem.batch
                wj_row = conn.execute(text("""
                    SELECT workflowjobid FROM public.workflowjob
                    WHERE workflowid = :wid AND jobname = 'Hydrochemistry' LIMIT 1
                """), {"wid": wid}).fetchone()
                wjid = wj_row[0] if wj_row else None

                # Insert chem.batch header
                new_run_id = conn.execute(text("""
                    INSERT INTO chem.batch
                        (workflowid, workflowjobid, procedureid, equipmentid,
                         islocked, runstatus, runstarttime, createdatestamp, createuserstamp)
                    VALUES (:wid, :jid, :pid, :eid,
                            FALSE, 6, :now, :now, :user)
                    RETURNING runid
                """), {"wid": wid, "jid": wjid, "pid": pid, "eid": eid,
                       "now": now, "user": user}).scalar()

                # Insert public.analysis + chem.loadlist per sample
                for pos, pfx, sid in samples:
                    aid = conn.execute(text("""
                        INSERT INTO public.analysis
                            (prefix, sampleid, workflowid, status, repeats, createdatestamp, createuserstamp)
                        VALUES (:pfx, :sid, :wid, 6, :rep, :now, :user)
                        RETURNING analysisid
                    """), {"pfx": pfx, "sid": sid, "wid": wid,
                           "rep": repeats, "now": now, "user": user}).scalar()

                    conn.execute(text("""
                        INSERT INTO chem.loadlist
                            (runid, analysisid, positionno, status, repeat)
                        VALUES (:rid, :aid, :pos, 6, 1)
                    """), {"rid": new_run_id, "aid": aid, "pos": pos})

                    # Mark sample as In Progress
                    conn.execute(text(
                        "UPDATE public.sample SET status = 200 WHERE sampleid = :sid AND prefix = :pfx"
                    ), {"sid": sid, "pfx": pfx})

                conn.commit()

            self.created_run_id = new_run_id
            show_message(self, "Created",
                         f"Pre-Analysis Batch {new_run_id} created with {len(samples)} samples.",
                         QMessageBox.Information)
            self.accept()

        except Exception as exc:
            logging.error("PreAnalysisCreate._on_create: %s", exc)
            show_message(self, "Error", f"Failed to create batch:\n{exc}", QMessageBox.Critical)
