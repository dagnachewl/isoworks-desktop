"""
validation_engine.py
====================
Module-agnostic acceptance-checking engine for the unified validation workflow.

Responsibilities
----------------
* Call the appropriate stored procedure (fn_check_siam/lsc/ngam_acceptance)
  for a given module + run, returning structured AnalysisOutcome objects.
* Expose privilege helpers (can_validate, can_override).
* Write validator decisions (VALIDATED / OVERRIDE / REPEAT_QUEUED / CANCELLED)
  to public.validation_log and update public.analysis.status accordingly.
* Post-validation hooks per module (run-level status updates, FinalValue
  promotion for LSC) are registered here and called by the GUI after sign-off.

This module has NO GUI dependencies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import numpy as np

from sqlalchemy import text

from db_core import db_manager
from shared_utils import get_current_user_id

log = logging.getLogger(__name__)

# ── qc_decision constants ─────────────────────────────────────────────────────
QC_PENDING        = 0
QC_PASS           = 1
QC_FAIL           = 2
QC_REPEAT_QUEUED  = 3
QC_OVERRIDE       = 4
QC_CANCELLED      = -1

# Analysis status codes
STATUS_REPEAT     = 99   # generic repeat — appears in create-run available list
STATUS_CANCELLED  = -9
STATUS_FINALIZED  = 210


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AnalysisOutcome:
    """Result of the acceptance check for one analysis (× measurable for SIAM)."""
    analysisid:     int
    labid:          str
    samplename:     str
    n_done:         int
    n_expected:     int
    repeat_status:  str           # 'DONE' | 'PENDING'
    metric_name:    str           # 'RPD' | 'SingleValue'
    metric_value:   Optional[float]
    threshold_pct:  float
    qc_decision:    int           # QC_* constant
    measurableid:   int = 0       # SIAM: per-measurable; 0 for LSC/NGAM
    parameterlabel: str = ""      # SIAM: e.g. 'δ²H'; empty for LSC/NGAM

    @property
    def repeats_complete(self) -> bool:
        return self.repeat_status == "DONE"

    @property
    def passed(self) -> Optional[bool]:
        """None = cannot evaluate yet (repeats pending)."""
        if not self.repeats_complete:
            return None
        return self.qc_decision in (QC_PASS, QC_OVERRIDE)

    @property
    def color(self) -> str:
        if self.qc_decision == QC_CANCELLED:
            return "grey"
        if self.qc_decision == QC_OVERRIDE:
            return "amber"
        if self.qc_decision == QC_REPEAT_QUEUED:
            return "blue"
        if not self.repeats_complete:
            return "amber"
        return "green" if self.qc_decision == QC_PASS else "red"

    @property
    def status_label(self) -> str:
        labels = {
            QC_PENDING:       "Pending",
            QC_PASS:          "Pass",
            QC_FAIL:          "Fail",
            QC_REPEAT_QUEUED: "Repeat Queued",
            QC_OVERRIDE:      "Override",
            QC_CANCELLED:     "Cancelled",
        }
        if not self.repeats_complete and self.qc_decision == QC_PENDING:
            return f"Pending ({self.n_done}/{self.n_expected} repeats)"
        return labels.get(self.qc_decision, "Unknown")


@dataclass
class RunQCSummary:
    """Aggregate QC status for a whole run."""
    run_id:   int
    module:   str
    outcomes: list[AnalysisOutcome] = field(default_factory=list)

    # Priority used to pick the worst outcome when deduplicating by analysisid.
    # Higher number = worse.  FAIL beats PENDING beats PASS, etc.
    _QCD_PRIORITY = {
        QC_FAIL: 5, QC_PENDING: 4, QC_REPEAT_QUEUED: 3,
        QC_OVERRIDE: 2, QC_PASS: 1, QC_CANCELLED: 0,
    }

    @property
    def unique_analyses(self) -> list[AnalysisOutcome]:
        """One outcome per analysisid, worst-case qc_decision across measurables."""
        seen: dict[int, AnalysisOutcome] = {}
        for o in self.outcomes:
            prev = seen.get(o.analysisid)
            if prev is None or (
                self._QCD_PRIORITY.get(o.qc_decision, 0)
                > self._QCD_PRIORITY.get(prev.qc_decision, 0)
            ):
                seen[o.analysisid] = o
        return list(seen.values())

    @property
    def n_pass(self)           -> int: return sum(1 for o in self.unique_analyses if o.qc_decision == QC_PASS)
    @property
    def n_fail(self)           -> int: return sum(1 for o in self.unique_analyses if o.qc_decision == QC_FAIL)
    @property
    def n_pending(self)        -> int: return sum(1 for o in self.unique_analyses if o.qc_decision == QC_PENDING)
    @property
    def n_repeat_queued(self)  -> int: return sum(1 for o in self.unique_analyses if o.qc_decision == QC_REPEAT_QUEUED)
    @property
    def n_override(self)       -> int: return sum(1 for o in self.unique_analyses if o.qc_decision == QC_OVERRIDE)
    @property
    def n_cancelled(self)      -> int: return sum(1 for o in self.unique_analyses if o.qc_decision == QC_CANCELLED)

    @property
    def ready_for_validation(self) -> bool:
        """True when every analysis (worst-case across measurables) is terminal."""
        return all(
            o.qc_decision in (QC_PASS, QC_OVERRIDE, QC_CANCELLED)
            for o in self.unique_analyses
        )

    @property
    def summary_text(self) -> str:
        parts = []
        if self.n_pass:          parts.append(f"{self.n_pass} Pass")
        if self.n_fail:          parts.append(f"{self.n_fail} Fail")
        if self.n_pending:       parts.append(f"{self.n_pending} Pending")
        if self.n_repeat_queued: parts.append(f"{self.n_repeat_queued} Repeat Queued")
        if self.n_override:      parts.append(f"{self.n_override} Override")
        if self.n_cancelled:     parts.append(f"{self.n_cancelled} Cancelled")
        return "  ·  ".join(parts) if parts else "No unknowns"


# ─────────────────────────────────────────────────────────────────────────────
# Stored-procedure callers
# ─────────────────────────────────────────────────────────────────────────────

_PROC_MAP = {
    "SIAM": "public.fn_check_siam_acceptance",
    "LSC":  "public.fn_check_lsc_acceptance",
    "NGAM": "public.fn_check_ngam_acceptance",
}


def run_acceptance_check(run_id: int, module: str) -> RunQCSummary:
    """
    Call the stored procedure for *module*, update qc_decision/repeat_status
    on the loadlist, and return a RunQCSummary.

    Raises ValueError for unknown module.
    """
    module = module.upper()
    proc   = _PROC_MAP.get(module)
    if proc is None:
        raise ValueError(f"Unknown module '{module}'. Expected one of {list(_PROC_MAP)}")

    outcomes: list[AnalysisOutcome] = []
    try:
        with db_manager.get_connection() as conn:
            rows = conn.execute(
                text(f"SELECT * FROM {proc}(:rid)"),
                {"rid": run_id},
            ).fetchall()
            conn.commit()   # stored proc did UPDATEs

        is_siam = (module == "SIAM")
        for r in rows:
            outcomes.append(AnalysisOutcome(
                analysisid    = int(r.analysisid),
                labid         = r.labid or "",
                samplename    = r.samplename or "",
                n_done        = int(r.n_done),
                n_expected    = int(r.n_expected),
                repeat_status = r.repeat_status or "PENDING",
                metric_name   = r.metric_name or "SingleValue",
                metric_value  = float(r.metric_value) if r.metric_value is not None else None,
                threshold_pct = float(r.threshold_pct) if r.threshold_pct is not None else 5.0,
                qc_decision   = int(r.qc_decision),
                measurableid  = int(r.measurableid) if is_siam else 0,
                parameterlabel= (r.parameterlabel or "") if is_siam else "",
            ))
    except Exception as exc:
        log.error("run_acceptance_check failed for %s run %d: %s", module, run_id, exc)
        raise

    return RunQCSummary(run_id=run_id, module=module, outcomes=outcomes)


# ─────────────────────────────────────────────────────────────────────────────
# Privilege helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_role_flags(login: str) -> dict:
    """Return role flags for the given system login name."""
    try:
        with db_manager.get_connection() as conn:
            row = conn.execute(text("""
                SELECT BOOL_OR(r.canvalidate)    AS canvalidate,
                       BOOL_OR(r.validatoradmin) AS validatoradmin
                FROM   public.employee      e
                JOIN   public.employee_role er ON er.employeeid = e.employeeid
                JOIN   public.role          r  ON r.roleid      = er.roleid
                WHERE  LOWER(e.systemloginname) = LOWER(:login)
            """), {"login": login}).fetchone()
        if row:
            return {
                "canvalidate":    bool(row.canvalidate),
                "validatoradmin": bool(row.validatoradmin),
            }
    except Exception as exc:
        log.warning("Could not fetch role flags for '%s': %s", login, exc)
    return {"canvalidate": False, "validatoradmin": False}


def get_current_user_flags() -> dict:
    login = get_current_user_id()
    return _fetch_role_flags(login)


def can_validate(login: str | None = None) -> bool:
    flags = _fetch_role_flags(login or get_current_user_id())
    return flags["canvalidate"] or flags["validatoradmin"]


def can_override(login: str | None = None) -> bool:
    flags = _fetch_role_flags(login or get_current_user_id())
    return flags["validatoradmin"]


# ─────────────────────────────────────────────────────────────────────────────
# Validator actions  (called by ValidationSignoffDialog)
# ─────────────────────────────────────────────────────────────────────────────

def _log_action(
    conn,
    analysisid:      int,
    run_id:          int,
    module:          str,
    action:          str,
    outcome:         AnalysisOutcome | None,
    employeeid:      int,
    signatoryname:   str,
    override_reason: str | None = None,
) -> None:
    conn.execute(text("""
        INSERT INTO public.validation_log
            (analysisid, runid, module, action,
             metric_name, metric_value, threshold_value, passed,
             override_reason, employeeid, signatoryname, signaturetime)
        VALUES
            (:aid, :rid, :mod, :act,
             :mname, :mval, :thresh, :passed,
             :reason, :eid, :sname, :now)
    """), {
        "aid":    analysisid,
        "rid":    run_id,
        "mod":    module,
        "act":    action,
        "mname":  outcome.metric_name   if outcome else None,
        "mval":   outcome.metric_value  if outcome else None,
        "thresh": outcome.threshold_pct if outcome else None,
        "passed": (action in ("VALIDATED", "OVERRIDE")) or None,
        "reason": override_reason,
        "eid":    employeeid,
        "sname":  signatoryname,
        "now":    datetime.now(),
    })


def apply_validator_decision(
    run_id:          int,
    module:          str,
    outcomes:        list[AnalysisOutcome],
    employeeid:      int,
    signatoryname:   str,
    overrides:       dict[int, str] | None = None,
) -> None:
    """
    Commit validator sign-off for all PASS (and OVERRIDE) analyses.

    *overrides* maps analysisid → override_reason for QC_FAIL outcomes
    that the validator has chosen to accept despite failure.

    Raises if any analysis is still PENDING or FAIL (without an override).
    """
    overrides = overrides or {}
    module    = module.upper()

    # Guard: must not have unresolved failures or pending repeats
    unresolved = [
        o for o in outcomes
        if o.qc_decision == QC_FAIL   and o.analysisid not in overrides
        or o.qc_decision == QC_PENDING
    ]
    if unresolved:
        ids = [o.analysisid for o in unresolved]
        raise ValueError(
            f"Cannot finalise: {len(unresolved)} analyses still pending/unresolved: {ids}"
        )

    with db_manager.get_connection() as conn:
        for outcome in outcomes:
            aid = outcome.analysisid
            if outcome.qc_decision == QC_CANCELLED:
                continue   # already handled; nothing to finalise

            action = "OVERRIDE" if aid in overrides else "VALIDATED"
            reason = overrides.get(aid)

            # Set analysis.status = 210 (Finalized)
            conn.execute(text("""
                UPDATE public.analysis SET status = :s
                WHERE  analysisid = :aid
            """), {"s": STATUS_FINALIZED, "aid": aid})

            _log_action(conn, aid, run_id, module, action, outcome,
                        employeeid, signatoryname, reason)

        conn.commit()

    # Call module post-hook (run-level status update, FinalValue promotion, etc.)
    hook = _POST_HOOKS.get(module)
    if hook:
        try:
            hook(run_id, employeeid, signatoryname)
        except Exception as exc:
            log.error("Post-validation hook for %s run %d failed: %s", module, run_id, exc)
            raise


def apply_repeat_decision(
    run_id:        int,
    module:        str,
    outcome:       AnalysisOutcome,
    employeeid:    int,
    signatoryname: str,
) -> None:
    """
    Validator decides: queue another repeat for a failing analysis.
    Sets public.analysis.status = 99 (Repeat) so it appears in create-run.
    Updates qc_decision on all loadlist rows for this analysis in this run.
    """
    module = module.upper()
    _LOADLIST = {
        "SIAM": ("siam.sianalysisloadlist",       "sianalysisrunid"),
        "LSC":  ("trims.lscloadlist",             "runid"),
        "NGAM": ("ngam.ng3hesequenceloadlist",    "runid"),
    }
    table, run_col = _LOADLIST[module]

    with db_manager.get_connection() as conn:
        conn.execute(text(f"""
            UPDATE {table}
            SET    qc_decision = {QC_REPEAT_QUEUED}
            WHERE  {run_col}  = :rid
              AND  analysisid = :aid
        """), {"rid": run_id, "aid": outcome.analysisid})

        conn.execute(text("""
            UPDATE public.analysis SET status = :s
            WHERE  analysisid = :aid
        """), {"s": STATUS_REPEAT, "aid": outcome.analysisid})

        _log_action(conn, outcome.analysisid, run_id, module,
                    "REPEAT_QUEUED", outcome, employeeid, signatoryname)
        conn.commit()


def apply_cancel_decision(
    run_id:        int,
    module:        str,
    outcome:       AnalysisOutcome,
    employeeid:    int,
    signatoryname: str,
) -> None:
    """Validator cancels a failing analysis."""
    module = module.upper()
    _LOADLIST = {
        "SIAM": ("siam.sianalysisloadlist",    "sianalysisrunid"),
        "LSC":  ("trims.lscloadlist",          "runid"),
        "NGAM": ("ngam.ng3hesequenceloadlist", "runid"),
    }
    table, run_col = _LOADLIST[module]

    with db_manager.get_connection() as conn:
        conn.execute(text(f"""
            UPDATE {table}
            SET    qc_decision = {QC_CANCELLED}
            WHERE  {run_col}  = :rid
              AND  analysisid = :aid
        """), {"rid": run_id, "aid": outcome.analysisid})

        conn.execute(text("""
            UPDATE public.analysis SET status = :s
            WHERE  analysisid = :aid
        """), {"s": STATUS_CANCELLED, "aid": outcome.analysisid})

        _log_action(conn, outcome.analysisid, run_id, module,
                    "CANCELLED", outcome, employeeid, signatoryname)
        conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Post-validation hooks  (registered by each module at import time)
# ─────────────────────────────────────────────────────────────────────────────

_POST_HOOKS: dict[str, Callable] = {}


def register_post_hook(module: str, fn: Callable[[int, int, str], None]) -> None:
    """
    Register a post-validation callable for *module*.

    Signature: fn(run_id: int, employeeid: int, signatoryname: str) -> None
    """
    _POST_HOOKS[module.upper()] = fn


def _siam_post_hook(run_id: int, employeeid: int, signatoryname: str) -> None:
    with db_manager.get_connection() as conn:
        conn.execute(text("""
            UPDATE siam.sianalysisrun
            SET    runendtime = :now, runstatus = 2
            WHERE  sianalysisrunid = :rid AND runendtime IS NULL
        """), {"now": datetime.now(), "rid": run_id})

        # Weighted mean across ALL finalized SIAM runs for analyses accepted in
        # this run. Weights are 1/σ² (inverse-variance). When σ=0, falls back to
        # simple mean. ON CONFLICT DO UPDATE means each successive run Finalise
        # replaces the stored aggregate with the new multi-run mean.
        conn.execute(text("""
            INSERT INTO public.finalvalue
                (analysisid, analyteid, measurableunit, fvalue, fvalueunc,
                 lldstatus, rejectflag, createdatestamp, createuserstamp)
            SELECT
                q.analysisid,
                q.measurableid::SMALLINT,
                q.unitid,
                CASE WHEN MIN(q.fvalueunc) > 0
                    THEN SUM(q.fvalue / (q.fvalueunc * q.fvalueunc))
                       / SUM(1.0   / (q.fvalueunc * q.fvalueunc))
                    ELSE AVG(q.fvalue)
                END,
                CASE WHEN MIN(q.fvalueunc) > 0
                    THEN SQRT(1.0 / SUM(1.0 / (q.fvalueunc * q.fvalueunc)))
                    ELSE SQRT(AVG(q.fvalueunc * q.fvalueunc))
                END,
                NULL,
                FALSE,
                NOW(),
                :usr
            FROM (
                SELECT ll.analysisid,
                       res.measurableid,
                       an.unitid,
                       res.fvalue,
                       res.fvalueunc
                FROM   siam.sianalysisloadlist ll
                JOIN   siam.sianalysisrun       r   ON r.sianalysisrunid = ll.sianalysisrunid
                JOIN   siam.sianalysisresult    res ON res.sianalysisid  = ll.sianalysisid
                JOIN   public.analytes          an  ON an.analyteid      = res.measurableid
                JOIN   public.analysis          a   ON a.analysisid      = ll.analysisid
                WHERE  ll.analysisid IN (
                           SELECT DISTINCT ll2.analysisid
                           FROM   siam.sianalysisloadlist ll2
                           WHERE  ll2.sianalysisrunid = :rid
                       )
                  AND  r.runstatus                   = 2
                  AND  COALESCE(ll.isignored,  FALSE) = FALSE
                  AND  COALESCE(res.isignored, FALSE) = FALSE
                  AND  res.fvalue IS NOT NULL
                  AND  a.status = :finalized
            ) q
            GROUP BY q.analysisid, q.measurableid, q.unitid
            ON CONFLICT (analysisid, analyteid) DO UPDATE SET
                measurableunit  = EXCLUDED.measurableunit,
                fvalue          = EXCLUDED.fvalue,
                fvalueunc       = EXCLUDED.fvalueunc,
                rejectflag      = EXCLUDED.rejectflag,
                createdatestamp = EXCLUDED.createdatestamp,
                createuserstamp = EXCLUDED.createuserstamp
        """), {"rid": run_id, "usr": signatoryname, "finalized": STATUS_FINALIZED})

        conn.commit()


def _lsc_post_hook(run_id: int, employeeid: int, signatoryname: str) -> None:
    """Lock the LSC run and promote results to FinalValue."""
    with db_manager.get_connection() as conn:
        conn.execute(text("""
            UPDATE trims.lscrun
            SET    islocked = TRUE, runstatus = 8, runendtime = :now
            WHERE  runid = :rid
        """), {"now": datetime.now(), "rid": run_id})

        conn.execute(text("""
            UPDATE trims.lscloadlist
            SET    status = 8
            WHERE  runid = :rid
              AND  COALESCE(isignored, FALSE) = FALSE
        """), {"rid": run_id})

        # Promote LSC activity → FinalValue (analyteid=3, Tritium on Water)
        conn.execute(text("""
            INSERT INTO public.finalvalue
                (analysisid, analyteid, measurableunit, fvalue, fvalueunc,
                 lldstatus, rejectflag, createdatestamp, createuserstamp)
            SELECT
                lr.analysisid,
                3,
                lr.activityunit,
                lr.finalactivity,
                lr.finalactivityunc,
                lr.lldstatus,
                FALSE,
                NOW(),
                :usr
            FROM trims.lscresult   lr
            JOIN trims.lscloadlist ll
              ON ll.analysisid = lr.analysisid AND ll.runid = lr.runid
            WHERE lr.runid = :rid
              AND COALESCE(ll.isignored, FALSE) = FALSE
            ON CONFLICT (analysisid, analyteid) DO UPDATE SET
                measurableunit = EXCLUDED.measurableunit,
                fvalue         = EXCLUDED.fvalue,
                fvalueunc      = EXCLUDED.fvalueunc,
                lldstatus      = EXCLUDED.lldstatus,
                rejectflag     = EXCLUDED.rejectflag
        """), {"rid": run_id, "usr": signatoryname})

        # Promote SIAM ²H weighted-mean results → FinalValue (MeasurableID=2)
        # for analyses linked via the deuterium enrichment path (EF method=1)
        deut_links = conn.execute(text("""
            SELECT DISTINCT de.preenrichmentid  AS pre_aid,
                            de.postenrichmentid AS post_aid
            FROM   trims.lscresult         lr
            JOIN   trims.lscloadlist       ll ON ll.analysisid = lr.analysisid
                                             AND ll.runid      = lr.runid
            JOIN   trims.electrolysis      e  ON e.analysisid  = lr.analysisid
            JOIN   trims.deuteriumenrichment de ON de.electrolysisid = e.electrolysisid
            WHERE  lr.runid = :rid
              AND  lr.enrichmentfactormethod = 1
              AND  COALESCE(ll.isignored, FALSE) = FALSE
              AND  (de.preenchrichmentid IS NOT NULL OR de.postenrichmentid IS NOT NULL)
        """), {"rid": run_id}).fetchall()

        siam_aids: set[int] = set()
        for dl in deut_links:
            if dl.pre_aid  is not None: siam_aids.add(int(dl.pre_aid))
            if dl.post_aid is not None: siam_aids.add(int(dl.post_aid))

        for siam_aid in siam_aids:
            siam_res = conn.execute(text("""
                SELECT res.fvalue, res.fvalueunc
                FROM   siam.sianalysisloadlist  ll2
                JOIN   siam.sianalysisrun        r   ON r.sianalysisrunid = ll2.sianalysisrunid
                JOIN   siam.sianalysisresult     res ON res.sianalysisid  = ll2.sianalysisid
                WHERE  ll2.analysisid    = :aid
                  AND  res.measurableid  = 2
                  AND  COALESCE(res.isignored,  FALSE) = FALSE
                  AND  COALESCE(ll2.isignored,  FALSE) = FALSE
                  AND  r.islocked = TRUE
            """), {"aid": siam_aid}).fetchall()

            if not siam_res:
                continue

            fvals = [float(r.fvalue or 0.0) for r in siam_res]
            funcs = [float(r.fvalueunc or 0.0) for r in siam_res]

            if len(fvals) == 1:
                wmean, wmean_unc = fvals[0], funcs[0]
            elif all(u > 0 for u in funcs):
                weights   = [1.0 / (u ** 2) for u in funcs]
                w_sum     = sum(weights)
                wmean     = sum(w * v for w, v in zip(weights, fvals)) / w_sum
                wmean_unc = (1.0 / w_sum) ** 0.5
            else:
                wmean     = float(np.mean(fvals))
                wmean_unc = 0.0

            conn.execute(text("""
                INSERT INTO public.finalvalue
                    (analysisid, analyteid, measurableunit, fvalue, fvalueunc,
                     lldstatus, rejectflag, createdatestamp, createuserstamp)
                VALUES (:aid, 2, NULL, :val, :unc, NULL, FALSE, NOW(), :usr)
                ON CONFLICT (analysisid, analyteid) DO UPDATE SET
                    fvalue     = EXCLUDED.fvalue,
                    fvalueunc  = EXCLUDED.fvalueunc,
                    rejectflag = EXCLUDED.rejectflag
            """), {"aid": siam_aid, "val": wmean, "unc": wmean_unc, "usr": signatoryname})

        conn.commit()


def _ngam_post_hook(run_id: int, employeeid: int, signatoryname: str) -> None:
    with db_manager.get_connection() as conn:
        conn.execute(text("""
            UPDATE ngam.ng3hesequencerun
            SET    runendtime = :now, runstatus = 5
            WHERE  runid = :rid AND runendtime IS NULL
        """), {"now": datetime.now(), "rid": run_id})
        conn.execute(text("""
            UPDATE ngam.ng3hesequenceloadlist
            SET    status = 5
            WHERE  runid = :rid
        """), {"rid": run_id})

        # Promote mean He_3_Water (analyteid=36) → public.finalvalue
        # Mean across repeated positions within the run (status=210 analyses only).
        conn.execute(text("""
            INSERT INTO public.finalvalue
                (analysisid, analyteid, measurableunit, fvalue, fvalueunc,
                 lldstatus, rejectflag, createdatestamp, createuserstamp)
            SELECT
                ll.analysisid,
                36::SMALLINT,
                NULL,
                AVG(res.activity_corrected),
                SQRT(AVG(res.activity_corrected_unc * res.activity_corrected_unc)),
                NULL,
                FALSE,
                NOW(),
                :usr
            FROM   ngam.ng3hesequenceloadlist  ll
            JOIN   ngam.ng3hesequenceresults   res
                   ON  res.runid         = ll.runid
                   AND res.positioninrun = ll.positioninrun
            JOIN   public.analysis             a  ON a.analysisid = ll.analysisid
            WHERE  ll.runid                       = :rid
              AND  COALESCE(ll.isrejected,  FALSE) = FALSE
              AND  COALESCE(res.isrejected, FALSE) = FALSE
              AND  res.activity_corrected IS NOT NULL
              AND  a.status = :finalized
            GROUP BY ll.analysisid
            ON CONFLICT (analysisid, analyteid) DO UPDATE SET
                fvalue    = EXCLUDED.fvalue,
                fvalueunc = EXCLUDED.fvalueunc,
                rejectflag = EXCLUDED.rejectflag
        """), {"rid": run_id, "usr": signatoryname, "finalized": STATUS_FINALIZED})

        conn.commit()


def _ngam_ms_post_hook(run_id: int, employeeid: int, signatoryname: str) -> None:
    """Lock the NGAM SMS/QMS run to status 210.

    Results remain in ngam.ngblockevaluation and ngam.ngratioresult.
    public.vw_final_results reads them directly — no finalvalue copy needed.
    """
    with db_manager.get_connection() as conn:
        conn.execute(text("""
            UPDATE ngam.msrun SET runstatus = 210 WHERE runid = :rid
        """), {"rid": run_id})
        conn.commit()


def accept_ngam_ms_run(
    run_id:        int,
    employeeid:    int,
    signatoryname: str,
) -> dict:
    """
    Finalise an NGAM SMS/QMS run: set all sample analyses to status 210,
    log each to public.validation_log, then run the post-hook to promote
    results to public.finalvalue.

    Returns {"finalised": N} where N is the number of analyses promoted.
    """
    with db_manager.get_connection() as conn:
        rows = conn.execute(text("""
            SELECT p.analysisid
            FROM   ngam.ngpreparations p
            JOIN   public.analysis     a ON a.analysisid = p.analysisid
            WHERE  p.runid       = :rid
              AND  a.sampleid IS NOT NULL
              AND  a.status     IN (8, 9)
        """), {"rid": run_id}).fetchall()

        aids = [r[0] for r in rows]
        if not aids:
            raise ValueError(f"No evaluated sample analyses found for NGAM run {run_id}")

        for aid in aids:
            conn.execute(text("""
                UPDATE public.analysis SET status = :s WHERE analysisid = :aid
            """), {"s": STATUS_FINALIZED, "aid": aid})
            _log_action(conn, aid, run_id, "NGAM_MS", "VALIDATED",
                        None, employeeid, signatoryname)

        conn.commit()

    _ngam_ms_post_hook(run_id, employeeid, signatoryname)
    return {"finalised": len(aids)}


# Register built-in hooks
register_post_hook("SIAM",    _siam_post_hook)
register_post_hook("LSC",     _lsc_post_hook)
register_post_hook("NGAM",    _ngam_post_hook)
register_post_hook("NGAM_MS", _ngam_ms_post_hook)
