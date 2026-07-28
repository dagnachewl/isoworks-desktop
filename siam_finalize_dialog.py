"""
siam_finalize_dialog.py
=======================
SIAM Run Finalization Dialog — PyQt5 replication of sfrmSampleAnalysisProperties.

Workflow
--------
1.  Opens for a specific SIAnalysisRunID.
2.  Loads all *unknown-sample* (SampleType = 0) analyses assigned to the run.
3.  For each AnalysisID it checks:
        done_repeats  = COUNT(SIAnalysisLoadList rows for this AnalysisID in this run)
        required      = Analysis.Repeats
        criteria_ok   = RPD of fValue across repeats < RPD_THRESHOLD (default 5 %)
4.  Row is colour-coded:
        green  = all repeats done + criteria met  → default action: Finalize
        yellow = repeats incomplete               → default action: Repeat Same Vial
        orange = repeats done but criteria failed → default action: Repeat Same Vial
5.  User selects rows (checkbox column) and picks an action per row (combo in last col)
    OR uses the global action radio group to bulk-set all selected rows.
6.  "Apply to Selected" executes the action; "Finalize Run" closes the whole run once
    every unknown analysis has been finalized.

Actions
-------
  1 – Finalize          : accept this analysis; if all are finalized, close the run
  2 – Repeat Same Vial  : insert a new SIAnalysisLoadList row (next Repeat number)
  3 – Cancel + New      : mark current entries CANCELLED; increment Analysis.Repeats
  4 – Cancel            : cancel this analysis entirely (set Remarks = CANCELLED on LL)
"""

import logging

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QCheckBox, QButtonGroup, QRadioButton, QMessageBox,
    QHeaderView, QWidget, QAbstractItemView, QComboBox,
    QSplitter, QTextEdit, QFrame,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QBrush, QFont

from db_core import db_manager
from sqlalchemy import text

# ── palette ──────────────────────────────────────────────────────────────────
_COL_READY      = QColor("#c8e6c9")   # green  – ready to finalize
_COL_INCOMPLETE = QColor("#fff9c4")   # yellow – repeats not done
_COL_FAILED     = QColor("#ffccbc")   # orange – criteria failed
_COL_CANCELLED  = QColor("#e0e0e0")   # grey   – already cancelled / done

# ── action constants ──────────────────────────────────────────────────────────
ACT_FINALIZE         = 1
ACT_REPEAT_SAME_VIAL = 2
ACT_CANCEL_NEW       = 3
ACT_CANCEL           = 4

_ACTION_LABELS = {
    ACT_FINALIZE:         "Finalize",
    ACT_REPEAT_SAME_VIAL: "Repeat (Same Vial)",
    ACT_CANCEL_NEW:       "Cancel + New Repeat",
    ACT_CANCEL:           "Cancel",
}

# ── table column indices ──────────────────────────────────────────────────────
_COL_CHK    = 0
_COL_SAMPLE = 1
_COL_AID    = 2
_COL_REQ    = 3
_COL_DONE   = 4
_COL_RPD    = 5
_COL_STATUS = 6
_COL_ACTION = 7


class SiamFinalizeDialog(QDialog):
    """
    Finalization review dialog for a single SIAM Analysis Run.

    Parameters
    ----------
    run_id : int
        The SIAnalysisRunID to review.
    rpd_threshold : float
        Maximum acceptable RPD (%) between repeat fValues.  Default 5 %.
    """

    RPD_THRESHOLD = 5.0   # % — can be overridden per-instance

    def __init__(self, run_id: int, rpd_threshold: float = None, parent=None):
        super().__init__(parent)
        self.run_id = run_id
        if rpd_threshold is not None:
            self.RPD_THRESHOLD = rpd_threshold

        self.setWindowTitle(f"Finalize SIAM Run {run_id}")
        self.resize(1050, 620)
        self.setMinimumWidth(800)

        self._build_ui()
        self._load_data()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ---- header row ---------------------------------------------------- #
        hdr = QHBoxLayout()
        self.lblRunInfo = QLabel()
        self.lblRunInfo.setFont(QFont("", 10, QFont.Bold))
        self.chkSelectAll = QCheckBox("Select All")
        self.chkSelectAll.toggled.connect(self._toggle_select_all)
        hdr.addWidget(self.lblRunInfo)
        hdr.addStretch()
        hdr.addWidget(QLabel(f"RPD threshold: <b>{self.RPD_THRESHOLD:.1f} %</b>"))
        hdr.addSpacing(16)
        hdr.addWidget(self.chkSelectAll)
        root.addLayout(hdr)

        # ---- main splitter: table (left) + result preview (right) ----------- #
        splitter = QSplitter(Qt.Horizontal)

        # -- analysis table --------------------------------------------------- #
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["✓", "Sample", "Analysis ID", "Req.", "Done", "RPD %", "Status", "Action"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(_COL_SAMPLE, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(_COL_STATUS, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(_COL_ACTION, QHeaderView.ResizeToContents)
        self.table.clicked.connect(self._on_row_clicked)
        lv.addWidget(self.table)
        splitter.addWidget(left)

        # -- result preview panel --------------------------------------------- #
        right = QGroupBox("Results Preview (selected analysis)")
        rv = QVBoxLayout(right)
        self.tblResults = QTableWidget(0, 4)
        self.tblResults.setHorizontalHeaderLabels(["Repeat", "Parameter", "Value", "Uncertainty"])
        self.tblResults.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblResults.verticalHeader().setVisible(False)
        self.tblResults.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        rv.addWidget(self.tblResults)

        self.chkAggregate = QCheckBox("Combine Replicates / In-Run Repeats")
        self.chkAggregate.setToolTip(
            "Average fValue and propagate uncertainty across all repeats of an analysis."
        )
        self.chkAggregate.toggled.connect(self._refresh_results_preview)
        rv.addWidget(self.chkAggregate)
        splitter.addWidget(right)

        splitter.setSizes([620, 380])
        root.addWidget(splitter, 1)

        # ---- colour legend -------------------------------------------------- #
        leg = QHBoxLayout()
        for color, txt in [
            (_COL_READY,      "Ready to Finalize"),
            (_COL_INCOMPLETE, "Incomplete Repeats"),
            (_COL_FAILED,     "Criteria Failed"),
            (_COL_CANCELLED,  "Cancelled / N/A"),
        ]:
            dot = QLabel("  ")
            dot.setStyleSheet(f"background:{color.name()}; border:1px solid #bbb; border-radius:2px;")
            dot.setFixedSize(16, 14)
            leg.addWidget(dot)
            leg.addWidget(QLabel(txt))
            leg.addSpacing(14)
        leg.addStretch()
        root.addLayout(leg)

        # ---- separator ------------------------------------------------------ #
        sep = QFrame(); sep.setFrameShape(QFrame.HLine); sep.setFrameShadow(QFrame.Sunken)
        root.addWidget(sep)

        # ---- global action radio group -------------------------------------- #
        act_grp = QGroupBox("Bulk Action for Selected Rows")
        ag = QHBoxLayout(act_grp)
        self._action_bg = QButtonGroup(self)
        for val, lbl in _ACTION_LABELS.items():
            rb = QRadioButton(lbl)
            self._action_bg.addButton(rb, val)
            ag.addWidget(rb)
        self._action_bg.button(ACT_FINALIZE).setChecked(True)
        self._action_bg.buttonClicked.connect(self._bulk_set_action)
        ag.addStretch()
        root.addWidget(act_grp)

        # ---- footer buttons ------------------------------------------------- #
        ftr = QHBoxLayout()

        self.btnFinalizeRun = QPushButton("Finalize Run")
        self.btnFinalizeRun.setToolTip(
            "Mark the entire run as complete.\n"
            "Enabled only when every unknown analysis is Ready to Finalize."
        )
        self.btnFinalizeRun.setStyleSheet("""
            QPushButton { background-color: #27ae60; color: white; font-weight: bold;
                          border: none; padding: 5px 16px; border-radius: 4px; }
            QPushButton:hover { background-color: #219a52; }
            QPushButton:disabled { background-color: #a9dfbf; color: #7f8c8d; }
        """)
        self.btnFinalizeRun.clicked.connect(self._finalize_whole_run)
        self.btnFinalizeRun.setEnabled(False)

        self.btnApply = QPushButton("Apply to Selected")
        self.btnApply.setStyleSheet("""
            QPushButton { background-color: #e67e22; color: white; font-weight: bold;
                          border: none; padding: 5px 16px; border-radius: 4px; }
            QPushButton:hover { background-color: #d35400; }
            QPushButton:disabled { background-color: #f0c080; color: #999; }
        """)
        self.btnApply.clicked.connect(self._apply_action)

        self.btnRefresh = QPushButton("Refresh")
        self.btnRefresh.clicked.connect(self._load_data)

        self.btnClose = QPushButton("Close")
        self.btnClose.clicked.connect(self.accept)

        ftr.addWidget(self.btnFinalizeRun)
        ftr.addStretch()
        ftr.addWidget(self.btnRefresh)
        ftr.addWidget(self.btnApply)
        ftr.addWidget(self.btnClose)
        root.addLayout(ftr)

    # ── data loading ──────────────────────────────────────────────────────────

    def _load_data(self):
        """Fetch unknown-sample analyses for this run with repeat counts + RPD."""
        sql = """
            SELECT
                a.AnalysisID,
                s.Prefix,
                s.SampleID,
                s.sName,
                COALESCE(a.Repeats, 1)         AS required_repeats,
                COUNT(ll.SIAnalysisID)          AS done_repeats
            FROM SIAM.SIAnalysisLoadList ll
            JOIN  Analysis a  ON ll.AnalysisID = a.AnalysisID
            JOIN  Sample   s  ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix
            WHERE ll.SIAnalysisRunID = :rid
              AND s.SampleType = 0
            GROUP BY a.AnalysisID, s.Prefix, s.SampleID, s.sName, a.Repeats
            ORDER BY a.AnalysisID
        """
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(sql), {"rid": self.run_id}).fetchall()
        except Exception as e:
            logging.error(f"FinalizeDialog load error: {e}")
            QMessageBox.critical(self, "Load Error", str(e))
            return

        self.table.setRowCount(0)
        all_ready = True

        for row in rows:
            analysis_id = int(row[0])
            prefix       = row[1] or ""
            sample_id    = row[2]
            sample_name  = row[3] or ""
            required     = int(row[4]) if row[4] else 1
            done         = int(row[5])

            rpd, rpd_txt, criteria_ok = self._check_acceptance(analysis_id, done)

            # ---- determine row state ---------------------------------------- #
            if done < required:
                status_text    = f"Incomplete ({done}/{required})"
                bg             = _COL_INCOMPLETE
                default_action = ACT_REPEAT_SAME_VIAL
                all_ready      = False
            elif not criteria_ok:
                status_text    = f"Criteria Failed (RPD {rpd:.1f}%)"
                bg             = _COL_FAILED
                default_action = ACT_REPEAT_SAME_VIAL
                all_ready      = False
            else:
                status_text    = "Ready to Finalize"
                bg             = _COL_READY
                default_action = ACT_FINALIZE

            r = self.table.rowCount()
            self.table.insertRow(r)

            # col 0: checkbox widget
            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk = QCheckBox()
            chk_layout.addWidget(chk)
            chk_layout.setAlignment(Qt.AlignCenter)
            chk_layout.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(r, _COL_CHK, chk_widget)

            # data columns
            self._set_item(r, _COL_SAMPLE, f"{prefix}-{sample_id}  {sample_name}", bg, analysis_id)
            self._set_item(r, _COL_AID,    str(analysis_id), bg, analysis_id)
            self._set_item(r, _COL_REQ,    str(required), bg)
            self._set_item(r, _COL_DONE,   str(done), bg)
            self._set_item(r, _COL_RPD,    rpd_txt, bg)
            self._set_item(r, _COL_STATUS, status_text, bg)

            # col ACTION: per-row combo
            cmb = QComboBox()
            for val, lbl in _ACTION_LABELS.items():
                cmb.addItem(lbl, val)
            cmb.setCurrentIndex(list(_ACTION_LABELS.keys()).index(default_action))
            self.table.setCellWidget(r, _COL_ACTION, cmb)

        self.table.resizeColumnsToContents()
        n = self.table.rowCount()
        self.lblRunInfo.setText(
            f"Run {self.run_id}  —  {n} unknown analysis(es)"
        )
        self.btnFinalizeRun.setEnabled(all_ready and n > 0)
        self._current_analysis_id = None
        self.tblResults.setRowCount(0)

    def _set_item(self, row, col, text, bg=None, user_data=None):
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        if bg:
            item.setBackground(QBrush(bg))
        if user_data is not None:
            item.setData(Qt.UserRole, user_data)
        self.table.setItem(row, col, item)

    # ── acceptance criteria ───────────────────────────────────────────────────

    def _check_acceptance(self, analysis_id: int, done_repeats: int):
        """
        Fetch fValues for this analysis in this run and compute RPD.

        Returns (rpd_float, rpd_display_str, passed_bool)
        RPD = (max − min) / |mean| × 100
        """
        if done_repeats < 2:
            return (0.0, "N/A", True)

        sql = """
            SELECT res.fValue
            FROM SIAM.SIAnalysisResult res
            JOIN SIAM.SIAnalysisLoadList ll ON res.SIAnalysisID = ll.SIAnalysisID
            WHERE ll.SIAnalysisRunID = :rid
              AND ll.AnalysisID      = :aid
              AND res.fValue IS NOT NULL
        """
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(
                    text(sql), {"rid": self.run_id, "aid": analysis_id}
                ).fetchall()
            values = [float(r[0]) for r in rows if r[0] is not None]
        except Exception as e:
            logging.warning(f"RPD check failed for analysis {analysis_id}: {e}")
            return (0.0, "?", True)

        if len(values) < 2:
            return (0.0, "N/A", True)

        mean = sum(values) / len(values)
        if abs(mean) < 1e-12:
            return (0.0, "0.0", True)

        rpd = (max(values) - min(values)) / abs(mean) * 100.0
        passed = rpd <= self.RPD_THRESHOLD
        return (rpd, f"{rpd:.2f}", passed)

    # ── row click → results preview ───────────────────────────────────────────

    def _on_row_clicked(self, index):
        item = self.table.item(index.row(), _COL_AID)
        if item:
            aid = item.data(Qt.UserRole)
            if aid and aid != getattr(self, "_current_analysis_id", None):
                self._current_analysis_id = aid
                self._refresh_results_preview()

    def _refresh_results_preview(self):
        aid = getattr(self, "_current_analysis_id", None)
        self.tblResults.setRowCount(0)
        if not aid:
            return

        aggregate = self.chkAggregate.isChecked()

        if aggregate:
            sql = """
                SELECT
                    'AVG' AS repeat_lbl,
                    m.ParameterLabel,
                    AVG(res.fValue)   AS val,
                    AVG(res.fValueUnc) AS unc
                FROM SIAM.SIAnalysisResult res
                JOIN Measurables m ON res.MeasurableID = m.MeasurableID
                JOIN SIAM.SIAnalysisLoadList ll ON res.SIAnalysisID = ll.SIAnalysisID
                WHERE ll.SIAnalysisRunID = :rid
                  AND ll.AnalysisID      = :aid
                GROUP BY m.ParameterLabel
                ORDER BY m.ParameterLabel
            """
        else:
            sql = """
                SELECT
                    ll.Repeat,
                    m.ParameterLabel,
                    res.fValue,
                    res.fValueUnc
                FROM SIAM.SIAnalysisResult res
                JOIN Measurables m ON res.MeasurableID = m.MeasurableID
                JOIN SIAM.SIAnalysisLoadList ll ON res.SIAnalysisID = ll.SIAnalysisID
                WHERE ll.SIAnalysisRunID = :rid
                  AND ll.AnalysisID      = :aid
                ORDER BY ll.Repeat, m.ParameterLabel
            """
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(
                    text(sql), {"rid": self.run_id, "aid": aid}
                ).fetchall()
        except Exception as e:
            logging.warning(f"Results preview failed: {e}")
            return

        for row in rows:
            r = self.tblResults.rowCount()
            self.tblResults.insertRow(r)
            self.tblResults.setItem(r, 0, QTableWidgetItem(str(row[0]) if row[0] is not None else ""))
            self.tblResults.setItem(r, 1, QTableWidgetItem(str(row[1]) if row[1] is not None else ""))
            self.tblResults.setItem(r, 2, QTableWidgetItem(
                f"{row[2]:.5g}" if row[2] is not None else ""))
            self.tblResults.setItem(r, 3, QTableWidgetItem(
                f"{row[3]:.5g}" if row[3] is not None else ""))
        self.tblResults.resizeColumnsToContents()

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _toggle_select_all(self, checked: bool):
        for r in range(self.table.rowCount()):
            chk = self._row_checkbox(r)
            if chk:
                chk.setChecked(checked)

    def _bulk_set_action(self, button):
        """Set all selected rows' action combo to the chosen global action."""
        action_val = self._action_bg.id(button)
        action_idx = list(_ACTION_LABELS.keys()).index(action_val)
        for r in range(self.table.rowCount()):
            chk = self._row_checkbox(r)
            if chk and chk.isChecked():
                cmb = self.table.cellWidget(r, _COL_ACTION)
                if cmb:
                    cmb.setCurrentIndex(action_idx)

    def _row_checkbox(self, row: int):
        w = self.table.cellWidget(row, _COL_CHK)
        return w.findChild(QCheckBox) if w else None

    def _selected_rows(self):
        return [
            r for r in range(self.table.rowCount())
            if (chk := self._row_checkbox(r)) and chk.isChecked()
        ]

    def _analysis_id_for_row(self, row: int):
        item = self.table.item(row, _COL_AID)
        return item.data(Qt.UserRole) if item else None

    # ── apply actions ─────────────────────────────────────────────────────────

    def _apply_action(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.warning(self, "No Selection",
                                "Please tick at least one analysis row.")
            return

        errors, applied = [], []
        for r in rows:
            aid = self._analysis_id_for_row(r)
            if aid is None:
                continue
            cmb = self.table.cellWidget(r, _COL_ACTION)
            action = cmb.currentData() if cmb else ACT_FINALIZE
            try:
                with db_manager.get_connection() as conn:
                    self._execute_action(conn, aid, action)
                    conn.commit()
                applied.append(aid)
            except Exception as e:
                logging.error(f"Action failed for analysis {aid}: {e}")
                errors.append(f"Analysis {aid}: {e}")

        if errors:
            QMessageBox.warning(self, "Partial Failure", "\n".join(errors))
        if applied:
            QMessageBox.information(
                self, "Done",
                f"Action applied to {len(applied)} analysis(es).",
            )
        self._load_data()

    def _execute_action(self, conn, analysis_id: int, action: int):
        if action == ACT_FINALIZE:
            self._do_finalize(conn, analysis_id)
        elif action == ACT_REPEAT_SAME_VIAL:
            self._do_repeat_same_vial(conn, analysis_id)
        elif action == ACT_CANCEL_NEW:
            self._do_cancel_new(conn, analysis_id)
        elif action == ACT_CANCEL:
            self._do_cancel(conn, analysis_id)

    # ---- individual action implementations ---------------------------------- #

    def _do_finalize(self, conn, analysis_id: int):
        """
        Accept results for this analysis.
        Sets a FINALIZED marker on the LoadList rows for audit trail.
        Extend here to update Analysis.StatusID if your schema has that column.
        """
        conn.execute(
            text("""
                UPDATE SIAM.SIAnalysisLoadList
                SET Remarks = CASE
                    WHEN Remarks IS NULL OR Remarks = '' THEN 'FINALIZED'
                    ELSE Remarks || ' | FINALIZED'
                END
                WHERE SIAnalysisRunID = :rid
                  AND AnalysisID      = :aid
            """),
            {"rid": self.run_id, "aid": analysis_id},
        )

    def _do_repeat_same_vial(self, conn, analysis_id: int):
        """
        Add the next repeat entry to the load list for this run.
        The new row's Repeat number = current max + 1.
        Also increments Analysis.Repeats so the completion check reflects it.
        """
        row = conn.execute(
            text("""
                SELECT MAX(ll.Repeat)
                FROM SIAM.SIAnalysisLoadList ll
                WHERE ll.SIAnalysisRunID = :rid AND ll.AnalysisID = :aid
            """),
            {"rid": self.run_id, "aid": analysis_id},
        ).fetchone()
        next_repeat = (int(row[0]) if row and row[0] is not None else 0) + 1

        conn.execute(
            text("""
                INSERT INTO SIAM.SIAnalysisLoadList
                    (SIAnalysisRunID, AnalysisID, Repeat, Remarks)
                VALUES (:rid, :aid, :rep, 'REPEAT QUEUED')
            """),
            {"rid": self.run_id, "aid": analysis_id, "rep": next_repeat},
        )
        # bump required repeats so the check knows another one is expected
        conn.execute(
            text("UPDATE Analysis SET Repeats = :rep WHERE AnalysisID = :aid"),
            {"rep": next_repeat, "aid": analysis_id},
        )

    def _do_cancel_new(self, conn, analysis_id: int):
        """
        Mark existing load-list entries as CANCELLED and queue a fresh repeat.
        Equivalent to Access optCancel_NewRepeat.
        """
        conn.execute(
            text("""
                UPDATE SIAM.SIAnalysisLoadList
                SET Remarks = 'CANCELLED'
                WHERE SIAnalysisRunID = :rid AND AnalysisID = :aid
            """),
            {"rid": self.run_id, "aid": analysis_id},
        )
        # Add a fresh blank slot for the next repeat
        row = conn.execute(
            text("""
                SELECT MAX(ll.Repeat)
                FROM SIAM.SIAnalysisLoadList ll
                WHERE ll.AnalysisID = :aid
            """),
            {"aid": analysis_id},
        ).fetchone()
        next_repeat = (int(row[0]) if row and row[0] is not None else 0) + 1

        conn.execute(
            text("""
                INSERT INTO SIAM.SIAnalysisLoadList
                    (SIAnalysisRunID, AnalysisID, Repeat, Remarks)
                VALUES (:rid, :aid, :rep, 'NEW REPEAT')
            """),
            {"rid": self.run_id, "aid": analysis_id, "rep": next_repeat},
        )
        conn.execute(
            text("UPDATE Analysis SET Repeats = :rep WHERE AnalysisID = :aid"),
            {"rep": next_repeat, "aid": analysis_id},
        )

    def _do_cancel(self, conn, analysis_id: int):
        """
        Cancel this analysis entirely.
        Marks all load-list entries CANCELLED.  Does not create a new repeat slot.
        """
        conn.execute(
            text("""
                UPDATE SIAM.SIAnalysisLoadList
                SET Remarks = 'CANCELLED'
                WHERE SIAnalysisRunID = :rid AND AnalysisID = :aid
            """),
            {"rid": self.run_id, "aid": analysis_id},
        )

    # ── finalize whole run ────────────────────────────────────────────────────

    def _finalize_whole_run(self):
        """
        Close the run: set RunEndTime = now if not set, update RunStatus.
        Only callable when all unknown analyses are Ready to Finalize.
        """
        reply = QMessageBox.question(
            self, "Finalize Run",
            f"Finalize Run {self.run_id}?\n\n"
            "This will set the Run End Time and mark the run as Complete.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            with db_manager.get_connection() as conn:
                conn.execute(
                    text("""
                        UPDATE SIAM.SIAnalysisRun
                        SET RunEndTime = CURRENT_TIMESTAMP,
                            RunStatus  = 2
                        WHERE SIAnalysisRunID = :rid
                          AND RunEndTime IS NULL
                    """),
                    {"rid": self.run_id},
                )
                conn.commit()
            QMessageBox.information(
                self, "Run Finalized",
                f"Run {self.run_id} has been marked as Complete.",
            )
            self.accept()
        except Exception as e:
            logging.error(f"Finalize run failed: {e}")
            QMessageBox.critical(self, "Error", str(e))
