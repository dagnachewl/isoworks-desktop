-- =============================================================================
-- PHASE REFACTOR MIGRATION
-- Replaces Analysis.Status (mixed TRIMS job-stage codes) with Analysis.Phase
-- (coarse 6-bucket summary: -1, 0..5)
--
-- Run order:
--   Step 1  – PhaseLookup table + data
--   Step 2  – job_procedure extensions
--   Step 3  – Analysis.Phase column + backfill
--   Step 4  – Rebuild vwSampleAnalysisStatus
--   Step 5  – fn_recalculate_phase()
--   Step 6  – Trigger wrappers (one per run table)
--   Step 7  – Verification queries
--
-- After smoke-testing, drop Analysis.Status in a separate maintenance window.
-- =============================================================================

BEGIN;

-- ============================================================================
-- STEP 1 — PhaseLookup
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.phaselookup (
    phase       SMALLINT    NOT NULL,
    name        VARCHAR(20) NOT NULL,   -- internal key (never shown to user)
    label       VARCHAR(40) NOT NULL,   -- UI display string
    sort_order  SMALLINT    NOT NULL,
    PRIMARY KEY (phase)
);

-- Truncate so this script is idempotent on re-run
TRUNCATE public.phaselookup;

INSERT INTO public.phaselookup (phase, name, label, sort_order) VALUES
    ( 0, 'Registered', 'Registered',   1),
    ( 1, 'Pending',    'Pending',       2),
    ( 2, 'Ongoing',    'Ongoing',       3),
    ( 3, 'Analysed',   'Analysed',      4),
    ( 4, 'Evaluated',  'Evaluated',     5),
    ( 5, 'Reported',   'Reported',      6),
    (-1, 'Cancelled',  'Cancelled',     7);


-- ============================================================================
-- STEP 2 — Extend job_procedure with phase-routing columns
--
-- NOTE: existing columns (tablename, fieldname, procedurename) serve the
-- procedure-settings m:m purpose and are LEFT UNCHANGED.
-- New columns carry run-table / load-list routing for the phase trigger only.
--
-- Actual job_procedure data (id | moduleid | sname):
--   1  | 1 | Purification_Method        → trims.primarydistillationbatch / trims.primarydistillation
--   2  | 1 | Electrolysis_Enrichment    → trims.electrolysisrun           / trims.electrolysis
--   3  | 1 | LSC                        → trims.lscrun                    / trims.lscloadlist
--   4  | 5 | Chemistry                  → (ChemistryBatch – no AnalysisID link yet, skip)
--   5  | 2 | Stable_Isotopes            → siam.sianalysisrun              / siam.sianalysisloadlist
--  10  | 3 | 3H_3He_Ingrowth            → (Ingrowth module – populate when ready)
--  11  | 3 | 3He_Ingrowth_Measurement   → (Ingrowth module – populate when ready)
--  12  | 4 | Noble_Gas_Extraction       → (Noble Gas module – populate when ready)
--  13  | 4 | Noble_Gas_Preparation      → (Noble Gas module – populate when ready)
--  14  | 4 | Noble_Gas_Measurement      → (Noble Gas module – populate when ready)
-- ============================================================================

ALTER TABLE public.job_procedure
    ADD COLUMN IF NOT EXISTS run_table         VARCHAR(100),   -- schema-qualified run table,      e.g. 'trims.electrolysisrun'
    ADD COLUMN IF NOT EXISTS run_endfld        VARCHAR(50),    -- "job closed" timestamp col name,  e.g. 'runendtime' or 'enddate'
    ADD COLUMN IF NOT EXISTS loadlist_table    VARCHAR(100),   -- schema-qualified load-list table, e.g. 'trims.electrolysis'
    ADD COLUMN IF NOT EXISTS loadlist_runfk    VARCHAR(50),    -- FK col on load list → run PK,     e.g. 'runid'
    ADD COLUMN IF NOT EXISTS loadlist_afield   VARCHAR(50),    -- AnalysisID col on load list,      e.g. 'analysisid'
    ADD COLUMN IF NOT EXISTS complete_statuses INTEGER[];      -- RunStatus values meaning "closed"

-- ── TRIMS: Purification (Distillation) ──────────────────────────────────────
-- primarydistillationbatch has no runstatus; completion = enddate IS NOT NULL
UPDATE public.job_procedure SET
    run_table         = 'trims.primarydistillationbatch',
    run_endfld        = 'enddate',
    loadlist_table    = 'trims.primarydistillation',
    loadlist_runfk    = 'runid',
    loadlist_afield   = 'analysisid',
    complete_statuses = NULL          -- completion detected via enddate, not runstatus
WHERE id = 1;  -- Purification_Method

-- ── TRIMS: Electrolysis Enrichment ──────────────────────────────────────────
UPDATE public.job_procedure SET
    run_table         = 'trims.electrolysisrun',
    run_endfld        = 'runendtime',
    loadlist_table    = 'trims.electrolysis',
    loadlist_runfk    = 'runid',
    loadlist_afield   = 'analysisid',
    complete_statuses = ARRAY[5]      -- 5 = Enriched
WHERE id = 2;  -- Electrolysis_Enrichment

-- ── TRIMS: LSC ──────────────────────────────────────────────────────────────
UPDATE public.job_procedure SET
    run_table         = 'trims.lscrun',
    run_endfld        = 'runendtime',
    loadlist_table    = 'trims.lscloadlist',
    loadlist_runfk    = 'runid',
    loadlist_afield   = 'analysisid',
    complete_statuses = ARRAY[7, 8]   -- 7 = Analyzed, 8 = Evaluated
WHERE id = 3;  -- LSC

-- ── SIAM: Stable Isotopes ────────────────────────────────────────────────────
UPDATE public.job_procedure SET
    run_table         = 'siam.sianalysisrun',
    run_endfld        = 'runendtime',
    loadlist_table    = 'siam.sianalysisloadlist',
    loadlist_runfk    = 'sianalysisrunid',
    loadlist_afield   = 'analysisid',
    complete_statuses = ARRAY[7, 8]   -- same as TRIMS: 7 = Analyzed, 8 = Evaluated
WHERE id = 5;  -- Stable_Isotopes

-- Chemistry (id=4), Ingrowth (id=10,11), Noble Gas (id=12,13,14):
-- loadlist_table left NULL → trigger skips them gracefully until populated.


-- ============================================================================
-- STEP 3 — Add Analysis.Phase, backfill from old Status codes
-- ============================================================================

ALTER TABLE public.analysis
    ADD COLUMN IF NOT EXISTS phase SMALLINT NOT NULL DEFAULT 0;

-- Backfill: map legacy Status → Phase
-- Old single-digit codes encode TRIMS instrument stages; mapped to coarse phases here.
UPDATE public.analysis SET phase = CASE
    WHEN status = -9                        THEN -1  -- Cancelled
    WHEN status = 1                         THEN  0  -- Registered
    WHEN status = 222                       THEN  1  -- Pending / TBA
    WHEN status IN (2, 3, 4, 5, 6,
                    29, 98, 99,             -- Repeat variants → still Ongoing
                    200, 201, 202,
                    203, 204, 205, 206,
                    999)                    THEN  2  -- Ongoing
    WHEN status IN (7, 210, 921, 922)       THEN  3  -- Analysed  (measurement done, not reviewed)
    WHEN status IN (8, 211, 923)            THEN  4  -- Evaluated (QC reviewed)
    WHEN status IN (9, 220, 221, 924)       THEN  5  -- Reported  (delivered to client)
    ELSE                                         0  -- fallback: Registered
END;

-- Sample.Status backfill (samples without an Analysis row use Sample.Status for phase)
-- No schema change needed on Sample — the view handles it via COALESCE.

-- ============================================================================
-- STEP 4 — Rebuild vwSampleAnalysisStatus to expose Phase
--
-- DROP first: CREATE OR REPLACE VIEW cannot reorder or insert columns in the
-- middle — PostgreSQL rejects it when existing column positions would shift.
-- The view is recreated with the same legacy columns preserved in order, with
-- the two new columns (phase, phase_label) appended at the end.
-- ============================================================================

DROP VIEW IF EXISTS public.vwsampleanalysisstatus;

CREATE VIEW public.vwsampleanalysisstatus AS
SELECT
    s.prefix,
    s.sampleid,
    s.sname                                         AS samplename,
    s.mediaid,
    CONCAT_WS('-', s.prefix, s.sampleid::text)      AS ourlabid,
    a.analysisid,
    s.submissionid,

    -- Legacy status (job-detail codes) — kept for fine-grained display
    COALESCE(a.status, s.status)                    AS status,
    stat.description,

    -- Legacy columns kept in original positions
    w.abbreviation::text AS abbreviation,
    a.repeats,
    a.precursoranalysisid,

    -- NEW: coarse phase (0–5, -1) — use this for charts and dashboards
    COALESCE(a.phase,
        CASE
            WHEN s.status = -9  THEN -1
            WHEN s.status = 222 THEN  1
            WHEN s.status = 1   THEN  0
            ELSE                     0
        END
    )                                               AS phase,
    pl.label                                        AS phase_label

FROM public.sample AS s
INNER JOIN public.submission AS sub
    ON s.submissionid = sub.submissionid
LEFT OUTER JOIN public.analysis AS a
    ON s.sampleid = a.sampleid AND s.prefix = a.prefix
LEFT OUTER JOIN public.statuslookup AS stat
    ON stat.status = COALESCE(a.status, s.status)
LEFT OUTER JOIN public.phaselookup AS pl
    ON pl.phase = COALESCE(a.phase,
        CASE
            WHEN s.status = -9  THEN -1
            WHEN s.status = 222 THEN  1
            WHEN s.status = 1   THEN  0
            ELSE                     0
        END)
LEFT JOIN public.workflow w
    ON a.workflowid = w.workflowid
WHERE sub.submissiontype <> 1;


-- ============================================================================
-- STEP 5 — fn_recalculate_phase(analysis_id)
-- Inspects all WorkflowJobs for the analysis's workflow and checks completion
-- via the routing data in job_procedure.
-- ============================================================================

CREATE OR REPLACE FUNCTION public.fn_recalculate_phase(p_analysis_id INTEGER)
RETURNS VOID
LANGUAGE plpgsql AS
$$
DECLARE
    v_workflow_id  SMALLINT;
    v_total        INTEGER := 0;
    v_complete     INTEGER := 0;
    v_new_phase    SMALLINT;
    rec            RECORD;
    dyn_sql        TEXT;
    job_done       BOOLEAN;
BEGIN
    SELECT workflowid INTO v_workflow_id
    FROM public.analysis
    WHERE analysisid = p_analysis_id;

    IF NOT FOUND THEN RETURN; END IF;

    FOR rec IN
        SELECT
            jp.run_table,
            jp.run_endfld,
            jp.loadlist_table,
            jp.loadlist_runfk,
            jp.loadlist_afield,
            jp.complete_statuses,
            wj.workflowjobid
        FROM public.workflowjob wj
        JOIN public.job_procedure jp ON jp.sname = wj.jobname
        WHERE wj.workflowid  = v_workflow_id
          AND wj.isobsolete  = false
          AND jp.loadlist_table IS NOT NULL   -- skip unroutable jobs (Chemistry, Ingrowth, Noble Gas for now)
    LOOP
        v_total := v_total + 1;

        -- Dynamic query: does a completed job row exist for this analysis?
        -- Two modes:
        --   a) runstatus-based (Electrolysis, LSC, SIAM): runstatus = ANY(complete_statuses)
        --   b) enddate-based   (Distillation):             run_endfld IS NOT NULL, complete_statuses IS NULL
        IF rec.complete_statuses IS NOT NULL THEN
            dyn_sql := format(
                'SELECT EXISTS (
                    SELECT 1
                    FROM  %s  ll
                    JOIN  %s  r   ON r.%I = ll.%I
                    WHERE ll.%I        = $1
                      AND r.workflowjobid = $2
                      AND r.runstatus    = ANY($3)
                      AND r.%I IS NOT NULL
                 )',
                rec.loadlist_table,
                rec.run_table,
                rec.loadlist_runfk,   -- r.<pk>  (dynamic: runid or sianalysisrunid)
                rec.loadlist_runfk,   -- ll.<fk> (same name on both sides)
                rec.loadlist_afield,
                rec.run_endfld
            );
            EXECUTE dyn_sql
                INTO job_done
                USING p_analysis_id, rec.workflowjobid, rec.complete_statuses;
        ELSE
            -- Completion = end-timestamp present (e.g. Distillation has no runstatus)
            dyn_sql := format(
                'SELECT EXISTS (
                    SELECT 1
                    FROM  %s  ll
                    JOIN  %s  r   ON r.%I = ll.%I
                    WHERE ll.%I        = $1
                      AND r.workflowjobid = $2
                      AND r.%I IS NOT NULL
                 )',
                rec.loadlist_table,
                rec.run_table,
                rec.loadlist_runfk,   -- r.<pk>  (dynamic: runid or sianalysisrunid)
                rec.loadlist_runfk,   -- ll.<fk> (same name on both sides)
                rec.loadlist_afield,
                rec.run_endfld
            );
            EXECUTE dyn_sql
                INTO job_done
                USING p_analysis_id, rec.workflowjobid;
        END IF;

        IF job_done THEN
            v_complete := v_complete + 1;
        END IF;
    END LOOP;

    -- Derive phase from job completion ratio
    v_new_phase := CASE
        WHEN v_total    = 0       THEN  0   -- Registered (no jobs in workflow)
        WHEN v_complete = 0       THEN  2   -- Ongoing (jobs exist, none closed)
        WHEN v_complete < v_total THEN  2   -- Ongoing (partial completion)
        ELSE                           3   -- Analysed (all jobs closed)
    END;

    -- Never downgrade terminal / manually-set phases
    UPDATE public.analysis
    SET    phase = v_new_phase
    WHERE  analysisid = p_analysis_id
      AND  phase NOT IN (-1, 4, 5);   -- Cancelled, Evaluated, Reported are terminal
END;
$$;


-- ============================================================================
-- STEP 6 — Trigger functions + triggers (one per run table)
-- ============================================================================

-- ── 6a. ElectrolysisRun  (job_procedure id=2, Electrolysis_Enrichment) ───────

CREATE OR REPLACE FUNCTION public.trg_electrolysisrun_phase_sync()
RETURNS TRIGGER LANGUAGE plpgsql AS
$$
BEGIN
    PERFORM public.fn_recalculate_phase(ll.analysisid)
    FROM trims.electrolysis ll          -- load list: trims.electrolysis
    WHERE ll.runid = NEW.runid;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_electrolysisrun_after_update ON trims.electrolysisrun;
CREATE TRIGGER trg_electrolysisrun_after_update
AFTER UPDATE OF runstatus, runendtime ON trims.electrolysisrun
FOR EACH ROW
WHEN (OLD.runstatus IS DISTINCT FROM NEW.runstatus
   OR OLD.runendtime IS DISTINCT FROM NEW.runendtime)
EXECUTE FUNCTION public.trg_electrolysisrun_phase_sync();


-- ── 6b. LSCRun  (job_procedure id=3, LSC) ───────────────────────────────────

CREATE OR REPLACE FUNCTION public.trg_lscrun_phase_sync()
RETURNS TRIGGER LANGUAGE plpgsql AS
$$
BEGIN
    PERFORM public.fn_recalculate_phase(ll.analysisid)
    FROM trims.lscloadlist ll
    WHERE ll.runid = NEW.runid;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_lscrun_after_update ON trims.lscrun;
CREATE TRIGGER trg_lscrun_after_update
AFTER UPDATE OF runstatus, runendtime ON trims.lscrun
FOR EACH ROW
WHEN (OLD.runstatus IS DISTINCT FROM NEW.runstatus
   OR OLD.runendtime IS DISTINCT FROM NEW.runendtime)
EXECUTE FUNCTION public.trg_lscrun_phase_sync();


-- ── 6c. PrimaryDistillationBatch  (job_procedure id=1, Purification_Method) ─
-- No runstatus column — fires on enddate being set.

CREATE OR REPLACE FUNCTION public.trg_distillationbatch_phase_sync()
RETURNS TRIGGER LANGUAGE plpgsql AS
$$
BEGIN
    PERFORM public.fn_recalculate_phase(ll.analysisid)
    FROM trims.primarydistillation ll
    WHERE ll.runid = NEW.runid;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_distillationbatch_after_update ON trims.primarydistillationbatch;
CREATE TRIGGER trg_distillationbatch_after_update
AFTER UPDATE OF enddate ON trims.primarydistillationbatch
FOR EACH ROW
WHEN (OLD.enddate IS DISTINCT FROM NEW.enddate AND NEW.enddate IS NOT NULL)
EXECUTE FUNCTION public.trg_distillationbatch_phase_sync();


-- ── 6d. SIAnalysisRun  (job_procedure id=5, Stable_Isotopes) ────────────────

CREATE OR REPLACE FUNCTION public.trg_sianalysisrun_phase_sync()
RETURNS TRIGGER LANGUAGE plpgsql AS
$$
BEGIN
    PERFORM public.fn_recalculate_phase(ll.analysisid)
    FROM siam.sianalysisloadlist ll
    WHERE ll.sianalysisrunid = NEW.sianalysisrunid;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sianalysisrun_after_update ON siam.sianalysisrun;
CREATE TRIGGER trg_sianalysisrun_after_update
AFTER UPDATE OF runstatus, runendtime ON siam.sianalysisrun
FOR EACH ROW
WHEN (OLD.runstatus IS DISTINCT FROM NEW.runstatus
   OR OLD.runendtime IS DISTINCT FROM NEW.runendtime)
EXECUTE FUNCTION public.trg_sianalysisrun_phase_sync();

-- ── 6e. Future modules (Ingrowth id=10,11 / Noble Gas id=12,13,14) ──────────
-- Add trigger functions here once their run/loadlist tables are confirmed.
-- fn_recalculate_phase() will include them automatically once job_procedure
-- rows are populated (loadlist_table is currently NULL → skipped).


-- ============================================================================
-- STEP 7 — Verification
-- ============================================================================

-- Phase distribution — compare to old Status distribution
SELECT
    a.phase,
    pl.label,
    COUNT(*) AS n
FROM public.analysis a
LEFT JOIN public.phaselookup pl ON pl.phase = a.phase
GROUP BY a.phase, pl.label, pl.sort_order
ORDER BY pl.sort_order;

-- Spot-check: any mismatch between expected phase and what was backfilled
SELECT
    a.analysisid,
    a.status        AS old_status,
    sl.description  AS old_description,
    a.phase         AS new_phase,
    pl.label        AS new_label
FROM public.analysis a
LEFT JOIN public.statuslookup sl ON sl.status = a.status
LEFT JOIN public.phaselookup  pl ON pl.phase  = a.phase
WHERE
    -- Reported statuses should be Phase 5
    (a.status IN (9, 220, 221, 924) AND a.phase <> 5)
    OR
    -- Evaluated statuses should be Phase 4
    (a.status IN (8, 211, 923)      AND a.phase <> 4)
    OR
    -- Analysed statuses should be Phase 3
    (a.status IN (7, 210, 921, 922) AND a.phase <> 3)
LIMIT 20;

-- Check job_procedure routing is complete for all active WorkflowJob types.
-- Rows with loadlist_table NULL are skipped by the trigger (modules not yet wired).
SELECT
    wj.jobname,
    jp.id,
    jp.sname,
    jp.run_table,
    jp.loadlist_table,
    CASE WHEN jp.loadlist_table IS NULL THEN '⚠ not routed' ELSE '✓' END AS routed
FROM public.workflowjob wj
LEFT JOIN public.job_procedure jp ON jp.sname = wj.jobname
WHERE wj.isobsolete = false
ORDER BY wj.jobname;

COMMIT;

-- =============================================================================
-- DEFERRED — run separately after smoke-test period:
--
--   ALTER TABLE public.analysis DROP COLUMN status;
--   -- (also remove from vwSampleAnalysisStatus and update all Python queries)
-- =============================================================================
