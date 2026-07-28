"""
validation_gui.py
=================
Two reusable Qt components for the unified validation workflow.

QCStatusWidget
    Embeds in any run-details window.  Shows a colour-coded acceptance table
    (repeat status, metric, pass/fail) for every unknown analysis in the run.
    • Analyst view  — read-only; shows traffic-light colours and summary bar.
    • Validator view — same table + action buttons (Repeat / Override / Cancel)
      and a "Validate & Sign" button that opens ValidationSignoffDialog.
    Mode is determined at runtime from the current user's role flags.

ValidationSignoffDialog
    Opened by QCStatusWidget when the validator clicks "Validate & Sign".
    Shows a concise read-only summary, a mandatory confirmation checkbox, and
    logs the signature to public.validation_log via validation_engine.
"""

from __future__ import annotations

import getpass
import logging
from datetime import datetime
from typing import Optional

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QAbstractItemView,
    QDialog, QDialogButtonBox, QTextEdit, QCheckBox, QFrame,
    QMessageBox, QSizePolicy, QComboBox, QSplitter, QGroupBox,
)
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont

from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from shared_utils import get_current_user_id
import validation_engine as ve
from validation_engine import (
    RunQCSummary, AnalysisOutcome,
    QC_PENDING, QC_PASS, QC_FAIL, QC_REPEAT_QUEUED, QC_OVERRIDE, QC_CANCELLED,
    run_acceptance_check, can_validate, can_override,
    apply_validator_decision, apply_repeat_decision, apply_cancel_decision,
)

log = logging.getLogger(__name__)

# ── Colour palette ────────────────────────────────────────────────────────────
_COL = {
    "green":  QColor("#C8E6C9"),
    "red":    QColor("#FFCDD2"),
    "amber":  QColor("#FFF3E0"),
    "blue":   QColor("#BBDEFB"),
    "grey":   QColor("#EEEEEE"),
    "white":  QColor("#FFFFFF"),
}
_DOT = {
    "green": "●", "red": "●", "amber": "●", "blue": "●", "grey": "●",
}
_DOT_CSS = {
    "green": "color:#2E7D32;", "red": "color:#C62828;",
    "amber": "color:#E65100;", "blue": "color:#1565C0;",
    "grey":  "color:#757575;",
}

_HDR_SS = (
    "QHeaderView::section {"
    "  background:#1F3A5F; color:white; font-weight:bold;"
    "  padding:3px 6px; border:none; border-right:1px solid #2E527A; }"
    "QHeaderView::section:last-section { border-right:none; }"
)
_BTN_SS  = ("QPushButton { background:%s; color:white; font-weight:bold; border:none;"
            " padding:4px 12px; border-radius:4px; }"
            "QPushButton:hover { background:%s; }"
            "QPushButton:disabled { background:#B0BEC5; color:#78909C; }")


def _btn(label: str, bg: str, hover: str) -> QPushButton:
    b = QPushButton(label)
    b.setStyleSheet(_BTN_SS % (bg, hover))
    b.setFixedHeight(28)
    return b


# ─────────────────────────────────────────────────────────────────────────────
# QCStatusWidget
# ─────────────────────────────────────────────────────────────────────────────

class QCStatusWidget(QWidget):
    """
    Embeddable QC-review panel for any run-details window.

    • Analyst view  — read-only colour-coded table + summary bar.
    • Validator view — same table with per-row Action dropdown, results preview
      panel, "Apply Actions" button, and "Validate & Sign" sign-off.

    Parameters
    ----------
    module : str
        'SIAM', 'LSC', or 'NGAM'.
    run_id : int
        The run whose analyses are being reviewed.
    parent_refresh : callable | None
        Called after a validator action so the parent window can reload.
    """

    validated = pyqtSignal()

    # Action dropdown values (on parent rows)
    _ACT_NONE     = 0
    _ACT_REPEAT   = 1
    _ACT_CANCEL   = 2
    _ACT_OVERRIDE = 3

    _ACT_LABELS = {
        _ACT_NONE:     "— no change —",
        _ACT_REPEAT:   "↻ Queue Repeat",
        _ACT_CANCEL:   "✕ Cancel",
        _ACT_OVERRIDE: "⚡ Override (accept)",
    }

    # Tree column indices (fixed, module-independent)
    _TC_ID     = 0   # Lab ID (parent) | Analysis ID (child)
    _TC_NAME   = 1   # Sample Name (parent) | Parameter (child)
    _TC_REPS   = 2   # Repeats
    _TC_METRIC = 3   # Metric name
    _TC_RPD    = 4   # RPD / Value
    _TC_STATUS = 5   # Status label
    _TC_ACTION = 6   # Action combo (validator, parent only)

    _TREE_HEADERS = ["Lab ID", "Name / Parameter", "Repeats", "Metric", "RPD / Value", "Status", "Action"]

    # Worst-case priority for deduplication (higher = worse)
    _QCD_PRIORITY = {
        QC_FAIL: 5, QC_PENDING: 4, QC_REPEAT_QUEUED: 3,
        QC_OVERRIDE: 2, QC_PASS: 1, QC_CANCELLED: 0,
    }

    def __init__(
        self,
        module: str,
        run_id: int,
        parent_refresh: Optional[callable] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._module         = module.upper()
        self._run_id         = run_id
        self._parent_refresh = parent_refresh
        self._summary: Optional[RunQCSummary] = None

        self._is_validator = can_validate()
        self._is_admin     = can_override()

        self._build_ui()
        self.refresh()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(4)

        # Header row
        hdr = QHBoxLayout()
        lbl = QLabel("QC Acceptance Review")
        lbl.setStyleSheet("font-weight:bold; font-size:12px; color:#1F3A5F;")
        hdr.addWidget(lbl)
        hdr.addStretch()
        self._lbl_summary = QLabel("")
        self._lbl_summary.setStyleSheet("font-size:11px; color:#555;")
        hdr.addWidget(self._lbl_summary)
        self._btn_expand = _btn("⊞ Expand All", "#607D8B", "#455A64")
        self._btn_expand.setMinimumWidth(
            self._btn_expand.fontMetrics().horizontalAdvance("⊟ Collapse All") + 24
        )
        self._btn_expand.clicked.connect(self._toggle_expand_all)
        hdr.addWidget(self._btn_expand)
        btn_refresh = _btn("↻ Refresh", "#607D8B", "#455A64")
        btn_refresh.setMinimumWidth(
            btn_refresh.fontMetrics().horizontalAdvance("↻ Refresh") + 24
        )
        btn_refresh.clicked.connect(self.refresh)
        hdr.addWidget(btn_refresh)
        lay.addLayout(hdr)

        # ── Splitter: analysis tree (top) + results preview (bottom) ─────────
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)

        # Analysis tree — parent rows per analysis, child rows per measurable
        self._tbl = QTreeWidget()
        self._tbl.setColumnCount(len(self._TREE_HEADERS))
        self._tbl.setHeaderLabels(self._TREE_HEADERS)
        self._tbl.header().setStyleSheet(_HDR_SS)
        self._tbl.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._tbl.header().setSectionResizeMode(self._TC_NAME, QHeaderView.Stretch)
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._tbl.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tbl.setRootIsDecorated(True)
        self._tbl.setIndentation(24)
        self._tbl.setAlternatingRowColors(False)
        self._tbl.setStyleSheet(
            "QTreeWidget { border:1px solid #C5CDD9; background:white; }"
            "QTreeWidget::item { padding:3px 5px; }"
        )
        self._tbl.currentItemChanged.connect(self._on_item_selected)
        splitter.addWidget(self._tbl)

        # Results preview panel
        preview_box = QGroupBox("Measurement Values  (parent row → means per parameter · child row → individual repeats)")
        pv = QVBoxLayout(preview_box)
        pv.setContentsMargins(4, 4, 4, 4)
        self._tbl_preview = QTableWidget(0, 4)
        self._tbl_preview.setHorizontalHeaderLabels(["Run", "Repeat / Label", "Value", "Uncertainty"])
        self._tbl_preview.horizontalHeader().setStyleSheet(_HDR_SS)
        self._tbl_preview.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._tbl_preview.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._tbl_preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._tbl_preview.setSelectionMode(QAbstractItemView.NoSelection)
        self._tbl_preview.verticalHeader().setVisible(False)
        self._tbl_preview.setStyleSheet(
            "QTableWidget { border:none; background:#FAFAFA; }"
            "QTableWidget::item { padding:2px 5px; }"
        )
        pv.addWidget(self._tbl_preview)
        splitter.addWidget(preview_box)

        splitter.setSizes([280, 160])
        lay.addWidget(splitter, 1)

        # ── Validator action bar ───────────────────────────────────────────────
        self._action_bar = QFrame()
        ab = QHBoxLayout(self._action_bar)
        ab.setContentsMargins(0, 4, 0, 2)
        ab.setSpacing(8)

        self._btn_apply   = _btn("▶  Apply Actions", "#1565C0", "#0D47A1")
        self._btn_signoff = _btn("✔  Validate & Sign", "#2E7D32", "#1B5E20")

        self._btn_apply.setToolTip(
            "Apply the action selected in the Action column for each row"
        )
        self._btn_signoff.setToolTip(
            "Finalise all passing / overridden analyses with your signature"
        )

        self._btn_apply.clicked.connect(self._on_apply_actions)
        self._btn_signoff.clicked.connect(self._on_signoff)

        ab.addWidget(self._btn_apply)
        ab.addStretch()
        ab.addWidget(self._btn_signoff)

        self._action_bar.setVisible(self._is_validator)
        lay.addWidget(self._action_bar)

    # ── Refresh ───────────────────────────────────────────────────────────────

    def refresh(self):
        try:
            self._summary = run_acceptance_check(self._run_id, self._module)
        except Exception as exc:
            log.error("QCStatusWidget.refresh failed: %s", exc)
            self._lbl_summary.setText(f"Error: {exc}")
            return

        self._populate_tree()
        self._update_summary_bar()
        self._update_button_states()
        self._tbl_preview.setRowCount(0)

    def _populate_tree(self):
        self._tbl.clear()
        outcomes = self._summary.outcomes if self._summary else []
        if not outcomes:
            return

        # Group outcomes by analysisid preserving first-seen order
        from collections import defaultdict
        groups: dict[int, list] = defaultdict(list)
        ordered = list(dict.fromkeys(o.analysisid for o in outcomes))
        for o in outcomes:
            groups[o.analysisid].append(o)

        bold_font = QFont(); bold_font.setBold(True)

        for aid in ordered:
            group = groups[aid]
            worst = max(group, key=lambda o: self._QCD_PRIORITY.get(o.qc_decision, 0))
            p_col = _COL.get(worst.color, _COL["white"])

            # ── Parent row ────────────────────────────────────────────────────
            parent = QTreeWidgetItem()
            parent.setData(0, Qt.UserRole, aid)
            parent.setData(0, Qt.UserRole + 1, worst)   # worst-case outcome

            parent.setText(self._TC_ID,     worst.labid)
            parent.setText(self._TC_NAME,   worst.samplename)
            parent.setText(self._TC_STATUS,
                           f"{_DOT.get(worst.color,'●')} {worst.status_label}")

            for col in range(len(self._TREE_HEADERS)):
                parent.setBackground(col, p_col)
                parent.setFont(col, bold_font)

            # Action combo on parent (validators only)
            if self._is_validator:
                any_actionable = any(
                    o.qc_decision not in (QC_CANCELLED, QC_REPEAT_QUEUED)
                    for o in group
                )
                any_fail = any(o.qc_decision == QC_FAIL for o in group)
                cmb = QComboBox()
                cmb.addItem(self._ACT_LABELS[self._ACT_NONE],     self._ACT_NONE)
                if any_actionable:
                    cmb.addItem(self._ACT_LABELS[self._ACT_REPEAT],  self._ACT_REPEAT)
                    cmb.addItem(self._ACT_LABELS[self._ACT_CANCEL],  self._ACT_CANCEL)
                if any_fail and self._is_admin:
                    cmb.addItem(self._ACT_LABELS[self._ACT_OVERRIDE], self._ACT_OVERRIDE)
                self._tbl.setItemWidget(parent, self._TC_ACTION, cmb)

            self._tbl.addTopLevelItem(parent)

            # ── Child rows — one per measurable ──────────────────────────────
            for o in group:
                c_col  = _COL.get(o.color, _COL["white"])
                rpd_txt = f"{o.metric_value:.2f}%" if o.metric_value is not None else "—"

                child = QTreeWidgetItem()
                child.setData(0, Qt.UserRole, o)   # full AnalysisOutcome

                child.setText(self._TC_ID,     str(o.analysisid))
                child.setText(self._TC_NAME,   o.parameterlabel or "—")
                child.setText(self._TC_REPS,
                              f"{o.n_done}/{o.n_expected}  {'✓' if o.repeats_complete else '…'}")
                child.setText(self._TC_METRIC, o.metric_name)
                child.setText(self._TC_RPD,    rpd_txt)
                child.setText(self._TC_STATUS,
                              f"{_DOT.get(o.color,'●')} {o.status_label}")

                for col in range(self._TC_ACTION):  # leave Action col blank
                    child.setBackground(col, c_col)

                parent.addChild(child)

            parent.setExpanded(True)

        self._tbl.resizeColumnToContents(self._TC_ID)
        self._btn_expand.setText("⊟ Collapse All")

    def _toggle_expand_all(self):
        expanding = self._btn_expand.text().startswith("⊞")
        if expanding:
            self._tbl.expandAll()
            self._btn_expand.setText("⊟ Collapse All")
        else:
            self._tbl.collapseAll()
            self._btn_expand.setText("⊞ Expand All")

    def _update_summary_bar(self):
        if self._summary:
            self._lbl_summary.setText(self._summary.summary_text)

    def _update_button_states(self):
        if not self._is_validator or not self._summary:
            return
        self._btn_apply.setEnabled(bool(self._summary.outcomes))
        self._btn_signoff.setEnabled(
            self._summary.ready_for_validation and bool(self._summary.outcomes)
        )

    # ── Results preview ───────────────────────────────────────────────────────

    def _on_item_selected(self, current: QTreeWidgetItem, _previous):
        self._tbl_preview.setRowCount(0)
        if current is None:
            return
        payload = current.data(0, Qt.UserRole)
        if isinstance(payload, AnalysisOutcome):
            # child row — detail for this measurable
            self._load_results_preview(payload.analysisid, payload.measurableid)
        elif isinstance(payload, int):
            # parent row — mean summary across all parameters
            self._load_mean_summary(payload)

    def _load_results_preview(self, analysis_id: int, measurableid: int = 0):
        """Query and display per-measurement values for the selected analysis × parameter."""
        module = self._module
        try:
            with db_manager.get_connection() as conn:
                if module == "SIAM":
                    rows = conn.execute(text("""
                        SELECT ll.sianalysisrunid::TEXT,
                               ll.repeat::TEXT,
                               res.fvalue,
                               res.fvalueunc
                        FROM   siam.sianalysisloadlist  ll
                        JOIN   siam.sianalysisresult    res ON res.sianalysisid = ll.sianalysisid
                        WHERE  ll.analysisid     = :aid
                          AND  res.measurableid  = :mid
                          AND  COALESCE(ll.isignored,  FALSE) = FALSE
                          AND  COALESCE(res.isignored, FALSE) = FALSE
                          AND  res.fvalue IS NOT NULL
                        ORDER BY ll.sianalysisrunid, ll.repeat
                    """), {"aid": analysis_id, "mid": measurableid}).fetchall()
                    self._tbl_preview.setHorizontalHeaderLabels(
                        ["Run", "Repeat", "Value", "Uncertainty"]
                    )
                elif module == "LSC":
                    rows = conn.execute(text("""
                        SELECT lr.runid::TEXT,
                               'Activity' AS lbl,
                               lr.finalactivity,
                               lr.finalactivityunc
                        FROM   trims.lscresult   lr
                        JOIN   trims.lscloadlist  ll ON ll.analysisid = lr.analysisid
                                                    AND ll.runid      = lr.runid
                        WHERE  lr.analysisid = :aid
                          AND  COALESCE(ll.isignored, FALSE) = FALSE
                          AND  lr.finalactivity IS NOT NULL
                        ORDER BY lr.runid
                    """), {"aid": analysis_id}).fetchall()
                    self._tbl_preview.setHorizontalHeaderLabels(
                        ["Run", "Measure", "Activity", "Uncertainty"]
                    )
                elif module == "NGAM":
                    rows = conn.execute(text("""
                        SELECT ll.runid::TEXT,
                               ll.positioninrun::TEXT,
                               res.activity_corrected,
                               res.activity_corrected_unc
                        FROM   ngam.ng3hesequenceloadlist  ll
                        JOIN   ngam.ng3hesequenceresults   res
                               ON  res.runid         = ll.runid
                               AND res.positioninrun = ll.positioninrun
                        WHERE  ll.analysisid = :aid
                          AND  COALESCE(ll.isrejected,  FALSE) = FALSE
                          AND  COALESCE(res.isrejected, FALSE) = FALSE
                          AND  res.activity_corrected IS NOT NULL
                        ORDER BY ll.runid
                    """), {"aid": analysis_id}).fetchall()
                    self._tbl_preview.setHorizontalHeaderLabels(
                        ["Run", "Position", "Activity (corr.)", "Uncertainty"]
                    )
                else:
                    return

        except Exception as exc:
            log.warning("Results preview failed for analysis %s: %s", analysis_id, exc)
            return

        self._tbl_preview.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                txt = (f"{float(val):.6g}" if isinstance(val, float) else str(val)) if val is not None else "—"
                it = QTableWidgetItem(txt)
                it.setFlags(Qt.ItemIsEnabled)
                self._tbl_preview.setItem(r, c, it)
        self._tbl_preview.resizeRowsToContents()

    def _load_mean_summary(self, analysis_id: int):
        """Show per-parameter mean ± propagated uncertainty for a parent row."""
        module = self._module
        self._tbl_preview.setHorizontalHeaderLabels(
            ["Parameter", "N", "Mean Value", "Uncertainty"]
        )
        try:
            with db_manager.get_connection() as conn:
                if module == "SIAM":
                    rows = conn.execute(text("""
                        SELECT m.parameterlabel,
                               COUNT(*)::INTEGER                             AS n,
                               AVG(res.fvalue)                              AS mean_val,
                               SQRT(AVG(res.fvalueunc * res.fvalueunc))     AS mean_unc
                        FROM   siam.sianalysisloadlist ll
                        JOIN   siam.sianalysisresult   res ON res.sianalysisid = ll.sianalysisid
                        JOIN   public.analytes         an  ON an.analyteid     = res.measurableid
                        WHERE  ll.sianalysisrunid        = :rid
                          AND  ll.analysisid             = :aid
                          AND  COALESCE(ll.isignored,  FALSE) = FALSE
                          AND  COALESCE(res.isignored, FALSE) = FALSE
                          AND  res.fvalue IS NOT NULL
                        GROUP BY res.measurableid, an.parameterlabel
                        ORDER BY res.measurableid
                    """), {"rid": self._run_id, "aid": analysis_id}).fetchall()
                elif module == "LSC":
                    rows = conn.execute(text("""
                        SELECT 'Activity'::TEXT,
                               COUNT(*)::INTEGER,
                               AVG(lr.finalactivity),
                               SQRT(AVG(lr.finalactivityunc * lr.finalactivityunc))
                        FROM   trims.lscresult   lr
                        JOIN   trims.lscloadlist  ll ON ll.analysisid = lr.analysisid
                                                    AND ll.runid      = lr.runid
                        WHERE  lr.analysisid = :aid
                          AND  ll.runid      = :rid
                          AND  COALESCE(ll.isignored, FALSE) = FALSE
                          AND  lr.finalactivity IS NOT NULL
                    """), {"rid": self._run_id, "aid": analysis_id}).fetchall()
                elif module == "NGAM":
                    rows = conn.execute(text("""
                        SELECT 'He_3_Water'::TEXT,
                               COUNT(*)::INTEGER,
                               AVG(res.activity_corrected),
                               SQRT(AVG(res.activity_corrected_unc * res.activity_corrected_unc))
                        FROM   ngam.ng3hesequenceloadlist  ll
                        JOIN   ngam.ng3hesequenceresults   res
                               ON  res.runid         = ll.runid
                               AND res.positioninrun = ll.positioninrun
                        WHERE  ll.runid      = :rid
                          AND  ll.analysisid = :aid
                          AND  COALESCE(ll.isrejected,  FALSE) = FALSE
                          AND  COALESCE(res.isrejected, FALSE) = FALSE
                          AND  res.activity_corrected IS NOT NULL
                    """), {"rid": self._run_id, "aid": analysis_id}).fetchall()
                else:
                    return
        except Exception as exc:
            log.warning("Mean summary failed for analysis %s: %s", analysis_id, exc)
            return

        self._tbl_preview.setRowCount(len(rows))
        for r, row in enumerate(rows):
            param, n, mean_val, mean_unc = row
            cells = [
                str(param) if param else "—",
                str(n) if n is not None else "—",
                f"{float(mean_val):.6g}" if mean_val is not None else "—",
                f"{float(mean_unc):.6g}" if mean_unc is not None else "—",
            ]
            for c, txt in enumerate(cells):
                it = QTableWidgetItem(txt)
                it.setFlags(Qt.ItemIsEnabled)
                self._tbl_preview.setItem(r, c, it)
        self._tbl_preview.resizeRowsToContents()

    # ── Validator actions ─────────────────────────────────────────────────────

    def _get_employeeid(self) -> tuple[int, str]:
        login = get_current_user_id()
        with db_manager.get_connection() as conn:
            row = conn.execute(text("""
                SELECT employeeid,
                       COALESCE(firstmiddlename || ' ', '') || lastname AS fullname
                FROM   public.employee
                WHERE  LOWER(systemloginname) = LOWER(:login)
            """), {"login": login}).fetchone()
        if row:
            return int(row.employeeid), row.fullname
        return 0, login

    def _on_apply_actions(self):
        """Read Action combo from each parent tree item and apply to its analysis."""
        if not self._summary:
            return

        to_repeat, to_cancel, override_outcomes = [], [], []

        for i in range(self._tbl.topLevelItemCount()):
            parent = self._tbl.topLevelItem(i)
            cmb = self._tbl.itemWidget(parent, self._TC_ACTION)
            if cmb is None:
                continue
            act = cmb.currentData()
            if act == self._ACT_NONE:
                continue
            worst: AnalysisOutcome = parent.data(0, Qt.UserRole + 1)
            if worst is None:
                continue
            if act == self._ACT_REPEAT:
                to_repeat.append(worst)
            elif act == self._ACT_CANCEL:
                to_cancel.append(worst)
            elif act == self._ACT_OVERRIDE:
                override_outcomes.append(worst)

        if not to_repeat and not to_cancel and not override_outcomes:
            show_message(self, "No Actions",
                         "No actions set. Use the Action dropdown on a row first.")
            return

        # Collect override reasons up-front
        to_override: dict[int, str] = {}
        for outcome in override_outcomes:
            dlg = _OverrideReasonDialog(outcome, self)
            if dlg.exec_() != QDialog.Accepted:
                return
            to_override[outcome.analysisid] = dlg.reason()

        try:
            eid, sname = self._get_employeeid()
            for outcome in to_repeat:
                apply_repeat_decision(self._run_id, self._module, outcome, eid, sname)
            for outcome in to_cancel:
                apply_cancel_decision(self._run_id, self._module, outcome, eid, sname)
            if to_override:
                apply_validator_decision(
                    run_id=self._run_id, module=self._module,
                    outcomes=override_outcomes, employeeid=eid,
                    signatoryname=sname, overrides=to_override,
                )
        except Exception as exc:
            show_message(self, "Error", str(exc))
            return

        self.refresh()
        if self._parent_refresh:
            self._parent_refresh()

        if self._module == "NGAM" and to_cancel:
            self._offer_duplicate_staging(to_cancel)

    def _offer_duplicate_staging(self, cancelled: list) -> None:
        """Query for field duplicates for each cancelled NGAM analysis and offer staging."""
        candidates = []
        try:
            with db_manager.get_connection() as conn:
                for outcome in cancelled:
                    orig = conn.execute(text("""
                        SELECT a.sampleid, a.prefix,
                               s.sName, s.SubmissionID, s.WorkflowID,
                               sub.PriorityID,
                               s.Prefix || '-' || CAST(s.SampleID AS VARCHAR) AS labid
                        FROM   public.analysis a
                        JOIN   Sample s  ON a.sampleid = s.sampleid AND a.prefix = s.prefix
                        JOIN   Submission sub ON s.SubmissionID = sub.SubmissionID
                        WHERE  a.analysisid = :aid
                    """), {"aid": outcome.analysisid}).fetchone()
                    if not orig:
                        continue

                    dups = conn.execute(text("""
                        SELECT s.SampleID, s.Prefix,
                               s.Prefix || '-' || CAST(s.SampleID AS VARCHAR) AS dup_labid,
                               s.sName,
                               COALESCE(CAST(s.CollectionDate AS VARCHAR), '') AS coldate,
                               COALESCE(ctl.type_name, '')                     AS ctype
                        FROM   Sample s
                        LEFT JOIN public.container_type_lookup ctl
                               ON ctl.container_type_id = s.container_type
                        LEFT JOIN public.sample_queue sq
                               ON s.SampleID = sq.sampleid AND s.Prefix = sq.prefix
                        LEFT JOIN public.analysis an
                               ON s.SampleID = an.SampleID AND s.Prefix = an.Prefix
                        WHERE  s.SubmissionID  = :sub_id
                          AND  s.DuplicateSample = TRUE
                          AND  sq.queue_id       IS NULL
                          AND  an.AnalysisID     IS NULL
                          AND  s.SampleType = 0
                    """), {"sub_id": orig.SubmissionID}).fetchall()

                    for d in dups:
                        candidates.append({
                            "orig_labid":    outcome.labid,
                            "orig_name":     outcome.samplename,
                            "dup_sampleid":  d[0],
                            "dup_prefix":    d[1],
                            "dup_labid":     d[2],
                            "dup_name":      d[3],
                            "dup_coldate":   d[4],
                            "dup_container": d[5],
                            "workflow_id":   orig.WorkflowID,
                            "priority_id":   orig.PriorityID,
                        })
        except Exception as exc:
            log.error("Duplicate staging lookup failed: %s", exc)
            return

        if not candidates:
            return

        _NgramDuplicateStageDialog(candidates, parent=self).exec_()

    def _on_signoff(self):
        if not self._summary:
            return
        dlg = ValidationSignoffDialog(
            summary=self._summary,
            module=self._module,
            run_id=self._run_id,
            parent=self,
        )
        if dlg.exec_() == QDialog.Accepted:
            self.validated.emit()
            self.refresh()
            if self._parent_refresh:
                self._parent_refresh()


# ─────────────────────────────────────────────────────────────────────────────
# Override reason dialog
# ─────────────────────────────────────────────────────────────────────────────

class _OverrideReasonDialog(QDialog):
    def __init__(self, outcome: AnalysisOutcome, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Override — Mandatory Reason")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)

        lay.addWidget(QLabel(
            f"<b>Analysis:</b> {outcome.labid} — {outcome.samplename}<br>"
            f"<b>Metric:</b> {outcome.metric_name} = "
            f"{'%.2f%%' % outcome.metric_value if outcome.metric_value else '—'} "
            f"(threshold {outcome.threshold_pct:.1f}%)<br><br>"
            "You are about to accept this result despite the QC failure.<br>"
            "A written justification is mandatory and will be logged."
        ))

        self._txt = QTextEdit()
        self._txt.setPlaceholderText("Enter justification for override…")
        self._txt.setMaximumHeight(100)
        lay.addWidget(self._txt)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._check_and_accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _check_and_accept(self):
        if len(self._txt.toPlainText().strip()) < 10:
            show_message(self, "Required",
                         "Please enter a justification (at least 10 characters).")
            return
        self.accept()

    def reason(self) -> str:
        return self._txt.toPlainText().strip()


# ─────────────────────────────────────────────────────────────────────────────
# _NgramDuplicateStageDialog
# ─────────────────────────────────────────────────────────────────────────────

class _NgramDuplicateStageDialog(QDialog):
    """Shown after NGAM cancellations — lists field duplicates available for TBA staging."""

    def __init__(self, candidates: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NGAM – Field Duplicate Available")
        self.setMinimumWidth(700)
        self.setMinimumHeight(300)
        self._candidates = candidates
        self._build_ui()

    def _build_ui(self):
        lay = QVBoxLayout(self)

        strip = QLabel("  One or more cancelled NGAM analyses have field duplicates available for staging.")
        strip.setFixedHeight(32)
        strip.setStyleSheet(
            "background:#4527A0; color:white; font-weight:bold; font-size:12px;"
        )
        lay.addWidget(strip)
        lay.addWidget(QLabel("Select duplicate(s) to stage to TBA, then click <b>Stage Selected</b>."))

        tbl = QTableWidget(len(self._candidates), 6)
        tbl.setHorizontalHeaderLabels([
            "Cancelled Sample", "Original Name",
            "Duplicate Lab ID", "Duplicate Name",
            "Collection Date", "Container Type",
        ])
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        tbl.setSelectionMode(QAbstractItemView.MultiSelection)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.verticalHeader().setVisible(False)

        for r, c in enumerate(self._candidates):
            it0 = QTableWidgetItem(c["orig_labid"])
            it0.setData(Qt.UserRole, c)
            tbl.setItem(r, 0, it0)
            tbl.setItem(r, 1, QTableWidgetItem(c["orig_name"]))
            tbl.setItem(r, 2, QTableWidgetItem(c["dup_labid"]))
            tbl.setItem(r, 3, QTableWidgetItem(c["dup_name"]))
            tbl.setItem(r, 4, QTableWidgetItem(c["dup_coldate"]))
            tbl.setItem(r, 5, QTableWidgetItem(c["dup_container"]))

        self._tbl = tbl
        lay.addWidget(tbl, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._btn_stage = QPushButton("Stage Selected to TBA")
        self._btn_stage.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;font-weight:bold;"
            "padding:6px 16px;border-radius:4px;}"
            "QPushButton:hover{background:#2ecc71;}"
        )
        self._btn_stage.clicked.connect(self._stage_selected)
        btn_skip = QPushButton("Skip")
        btn_skip.clicked.connect(self.reject)
        btn_row.addWidget(self._btn_stage)
        btn_row.addWidget(btn_skip)
        lay.addLayout(btn_row)

    def _stage_selected(self):
        rows = sorted(set(i.row() for i in self._tbl.selectionModel().selectedRows()))
        if not rows:
            show_message(self, "No Selection", "Select at least one row to stage.")
            return
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user_str = getpass.getuser()
        staged = []
        try:
            with db_manager.get_connection() as conn:
                for r in rows:
                    c = self._tbl.item(r, 0).data(Qt.UserRole)
                    wf  = c["workflow_id"]
                    pri = c["priority_id"]
                    sid = c["dup_sampleid"]
                    pfx = c["dup_prefix"]
                    job_row = conn.execute(text(
                        "SELECT WorkflowJobID FROM WorkflowJob "
                        "WHERE WorkflowID=:wf AND RunSequence=1"
                    ), {"wf": wf}).fetchone()
                    if not job_row:
                        show_message(
                            self, "Error",
                            f"No starting WorkflowJob for WorkflowID {wf}. Skipping."
                        )
                        continue
                    conn.execute(text("""
                        INSERT INTO public.sample_queue
                            (sampleid, prefix, mediaid, workflowjobid,
                             priorityid, repeat_count, queued_at, queued_by)
                        SELECT sampleid, prefix, mediaid, :job, :pri, 1, :now, :usr
                        FROM   public.sample
                        WHERE  sampleid = :sid AND prefix = :pfx
                        ON CONFLICT (sampleid, prefix, workflowjobid) DO NOTHING
                    """), {
                        "job": job_row[0], "pri": pri,
                        "now": now_str, "usr": user_str,
                        "sid": sid, "pfx": pfx,
                    })
                    conn.execute(text(
                        "UPDATE Sample SET Status=222 WHERE SampleID=:sid AND Prefix=:pfx"
                    ), {"sid": sid, "pfx": pfx})
                    staged.append(self._tbl.item(r, 2).text())
                conn.commit()
        except Exception as exc:
            show_message(self, "Error", f"Staging failed: {exc}")
            return
        show_message(self, "Staged", f"Staged to TBA: {', '.join(staged)}")
        self.accept()


# ─────────────────────────────────────────────────────────────────────────────
# ValidationSignoffDialog
# ─────────────────────────────────────────────────────────────────────────────

class ValidationSignoffDialog(QDialog):
    """
    Final sign-off dialog shown to the validator.

    Displays a read-only summary of all PASS / OVERRIDE analyses, requires an
    explicit confirmation tick, then calls apply_validator_decision().
    """

    def __init__(
        self,
        summary:  RunQCSummary,
        module:   str,
        run_id:   int,
        parent=None,
    ):
        super().__init__(parent)
        self._summary = summary
        self._module  = module
        self._run_id  = run_id
        self.setWindowTitle(f"Validate & Sign — {module} Run #{run_id}")
        self.setMinimumWidth(580)
        self._build_ui()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        # Title strip
        strip = QLabel(f"  Validator Sign-off — {self._module} Run #{self._run_id}")
        strip.setFixedHeight(32)
        strip.setStyleSheet(
            "background:#1F3A5F; color:white; font-weight:bold; font-size:13px;"
        )
        lay.addWidget(strip)

        # Summary table (read-only)
        tbl = QTableWidget(0, 5)
        tbl.setHorizontalHeaderLabels(
            ["Lab ID", "Sample Name", "Repeats", "RPD / Value", "Decision"]
        )
        tbl.horizontalHeader().setStyleSheet(_HDR_SS)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.setSelectionMode(QAbstractItemView.NoSelection)
        tbl.verticalHeader().setVisible(False)
        tbl.setMaximumHeight(240)

        # Only show analyses that will actually be finalized
        to_sign = [
            o for o in self._summary.outcomes
            if o.qc_decision in (QC_PASS, QC_OVERRIDE)
        ]
        tbl.setRowCount(len(to_sign))

        for row, o in enumerate(to_sign):
            col  = _COL["green"] if o.qc_decision == QC_PASS else _COL["amber"]
            rpd  = f"{o.metric_value:.2f}%" if o.metric_value is not None else "—"
            dec  = "Pass" if o.qc_decision == QC_PASS else "Override"
            for ci, txt in enumerate([
                o.labid, o.samplename,
                f"{o.n_done}/{o.n_expected}", rpd, dec,
            ]):
                it = QTableWidgetItem(txt)
                it.setFlags(Qt.ItemIsEnabled)
                it.setBackground(col)
                tbl.setItem(row, ci, it)

        lay.addWidget(tbl)

        # Also-being-cancelled note
        n_cancel = self._summary.n_cancelled
        n_repeat = self._summary.n_repeat_queued
        notes = []
        if n_cancel:
            notes.append(f"{n_cancel} analysis/analyses are cancelled and will be excluded.")
        if n_repeat:
            notes.append(f"{n_repeat} analysis/analyses have repeats queued and will not be finalised here.")
        if notes:
            lbl = QLabel("  ".join(notes))
            lbl.setStyleSheet("color:#777; font-size:11px;")
            lay.addWidget(lbl)

        # Signatory info
        frame = QFrame()
        frame.setStyleSheet("background:#EAF0F6; border:1px solid #AAB8CC; border-radius:4px;")
        fl = QHBoxLayout(frame)
        fl.addWidget(QLabel("Signing as:"))
        self._lbl_signatory = QLabel()
        self._lbl_signatory.setStyleSheet("font-weight:bold;")
        fl.addWidget(self._lbl_signatory, 1)
        lay.addWidget(frame)

        # Confirmation checkbox
        self._chk = QCheckBox(
            f"I confirm that I have reviewed {len(to_sign)} analyses "
            "and the results are acceptable for reporting."
        )
        self._chk.stateChanged.connect(self._update_ok)
        lay.addWidget(self._chk)

        # Buttons
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok_btn = btns.button(QDialogButtonBox.Ok)
        self._ok_btn.setText("Validate & Sign")
        self._ok_btn.setEnabled(False)
        self._ok_btn.setStyleSheet(_BTN_SS % ("#2E7D32", "#1B5E20"))
        btns.accepted.connect(self._do_sign)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        # Populate signatory
        try:
            login = get_current_user_id()
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT employeeid,
                           COALESCE(firstmiddlename || ' ', '') || lastname AS fullname
                    FROM   public.employee
                    WHERE  LOWER(systemloginname) = LOWER(:login)
                """), {"login": login}).fetchone()
            if row:
                self._employeeid   = int(row.employeeid)
                self._signatoryname = row.fullname
            else:
                self._employeeid    = 0
                self._signatoryname = login
        except Exception:
            self._employeeid    = 0
            self._signatoryname = get_current_user_id()

        self._lbl_signatory.setText(
            f"{self._signatoryname}  ({get_current_user_id()})"
        )
        self._to_sign = to_sign

    def _update_ok(self):
        self._ok_btn.setEnabled(self._chk.isChecked())

    def _do_sign(self):
        try:
            apply_validator_decision(
                run_id        = self._run_id,
                module        = self._module,
                outcomes      = self._to_sign,
                employeeid    = self._employeeid,
                signatoryname = self._signatoryname,
            )
            QMessageBox.information(
                self, "Signed Off",
                f"{len(self._to_sign)} analyses finalised successfully.\n"
                f"Signed by: {self._signatoryname}",
            )
            self.accept()
        except Exception as exc:
            log.error("ValidationSignoffDialog._do_sign failed: %s", exc)
            show_message(self, "Sign-off Failed", str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# QCReviewDialog — standalone dialog wrapping QCStatusWidget
# ─────────────────────────────────────────────────────────────────────────────

class QCReviewDialog(QDialog):
    """
    Standalone dialog containing a QCStatusWidget.
    Use this from run-list windows where the details window is not open.

    Parameters
    ----------
    module  : 'SIAM', 'LSC', or 'NGAM'
    run_id  : int  — the run to review
    """

    def __init__(self, module: str, run_id: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"QC / Validation — {module} Run #{run_id}")
        self.setMinimumSize(760, 440)
        self.resize(860, 500)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)

        strip = QLabel(f"  QC Acceptance Review — {module} Run #{run_id}")
        strip.setFixedHeight(30)
        strip.setStyleSheet(
            "background:#1F3A5F; color:white; font-weight:bold; font-size:13px;"
        )
        lay.addWidget(strip)

        self._qc = QCStatusWidget(
            module         = module,
            run_id         = run_id,
            parent_refresh = None,
            parent         = self,
        )
        self._qc.validated.connect(self.accept)
        lay.addWidget(self._qc, 1)

        btn_close = QPushButton("Close")
        btn_close.setFixedHeight(28)
        btn_close.clicked.connect(self.reject)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)
