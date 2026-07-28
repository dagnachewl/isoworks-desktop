-- =============================================================================
-- PHASE REFACTOR MIGRATION — SQL SERVER
-- Replaces Analysis.Status (mixed TRIMS job-stage codes) with Analysis.Phase
-- (coarse 7-bucket summary: -1, 0..5)
--
-- Requirements: SQL Server 2016+ (STRING_SPLIT, CREATE OR ALTER)
--
-- Run order:
--   Step 1  – PhaseLookup table + data
--   Step 2  – job_procedure extensions
--   Step 3  – Analysis.Phase column + backfill
--   Step 4  – Rebuild vwSampleAnalysisStatus
--   Step 5  – usp_RecalculatePhase stored procedure
--   Step 6  – Trigger wrappers (one per run table)
--   Step 7  – Verification queries
--
-- Key SQL Server differences from the PostgreSQL version:
--   • No array type  → complete_statuses stored as NVARCHAR comma-separated
--   • No FOR EACH ROW → triggers iterate via inserted/deleted with WHILE loop
--   • No CREATE OR REPLACE → CREATE OR ALTER (SQL Server 2016+)
--   • DDL inside TRY/CATCH → wrapped in EXEC() to avoid batch-separator issues
--   • QUOTENAME() used for safe dynamic identifier quoting
--   • STRING_SPLIT() used to parse complete_statuses
--
-- After smoke-testing, drop Analysis.Status in a separate maintenance window.
-- =============================================================================

SET NOCOUNT ON;
SET XACT_ABORT ON;   -- auto-rollback on any error

BEGIN TRANSACTION;

BEGIN TRY

-- ============================================================================
-- STEP 1 — PhaseLookup
-- ============================================================================

IF OBJECT_ID('dbo.PhaseLookup', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.PhaseLookup (
        Phase      SMALLINT     NOT NULL,
        Name       NVARCHAR(20) NOT NULL,   -- internal key (never shown to user)
        Label      NVARCHAR(40) NOT NULL,   -- UI display string
        SortOrder  SMALLINT     NOT NULL,
        CONSTRAINT PK_PhaseLookup PRIMARY KEY (Phase)
    );
END;

-- DELETE + re-insert so the script is idempotent on re-run
DELETE FROM dbo.PhaseLookup;

INSERT INTO dbo.PhaseLookup (Phase, Name, Label, SortOrder) VALUES
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
-- complete_statuses stored as NVARCHAR comma-separated (e.g. '7,8')
-- because SQL Server has no array type. Parsed via STRING_SPLIT() at runtime.
--
-- Actual job_procedure data (id | moduleid | sname):
--   1  | 1 | Purification_Method        → TRIMS.ElectrolysisRun (no RunStatus: EndDate-based)
--   2  | 1 | Electrolysis_Enrichment    → TRIMS.ElectrolysisRun  / TRIMS.Electrolysis
--   3  | 1 | LSC                        → TRIMS.LSCRun           / TRIMS.LSCLoadList
--   4  | 5 | Chemistry                  → (ChemistryBatch – no AnalysisID link yet, skip)
--   5  | 2 | Stable_Isotopes            → SIAM.SIAnalysisRun     / SIAM.SIAnalysisLoadList
--  10  | 3 | 3H_3He_Ingrowth            → (Ingrowth module – populate when ready)
--  11  | 3 | 3He_Ingrowth_Measurement   → (Ingrowth module – populate when ready)
--  12  | 4 | Noble_Gas_Extraction       → (Noble Gas module – populate when ready)
--  13  | 4 | Noble_Gas_Preparation      → (Noble Gas module – populate when ready)
--  14  | 4 | Noble_Gas_Measurement      → (Noble Gas module – populate when ready)
-- ============================================================================

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.job_procedure') AND name = 'run_table')
    ALTER TABLE dbo.job_procedure ADD run_table          NVARCHAR(100) NULL;  -- schema-qualified run table

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.job_procedure') AND name = 'run_endfld')
    ALTER TABLE dbo.job_procedure ADD run_endfld         NVARCHAR(50)  NULL;  -- closed-timestamp column name

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.job_procedure') AND name = 'loadlist_table')
    ALTER TABLE dbo.job_procedure ADD loadlist_table     NVARCHAR(100) NULL;  -- schema-qualified load-list table

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.job_procedure') AND name = 'loadlist_runfk')
    ALTER TABLE dbo.job_procedure ADD loadlist_runfk     NVARCHAR(50)  NULL;  -- FK col on load list → run PK

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.job_procedure') AND name = 'loadlist_afield')
    ALTER TABLE dbo.job_procedure ADD loadlist_afield    NVARCHAR(50)  NULL;  -- AnalysisID col on load list

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.job_procedure') AND name = 'complete_statuses')
    ALTER TABLE dbo.job_procedure ADD complete_statuses  NVARCHAR(100) NULL;  -- comma-separated RunStatus codes


-- ── TRIMS: Purification (Distillation) ──────────────────────────────────────
-- PrimaryDistillationBatch has no RunStatus; completion = EndDate IS NOT NULL
UPDATE dbo.job_procedure SET
    run_table          = 'TRIMS.PrimaryDistillationBatch',
    run_endfld         = 'EndDate',
    loadlist_table     = 'TRIMS.PrimaryDistillation',
    loadlist_runfk     = 'RunID',
    loadlist_afield    = 'AnalysisID',
    complete_statuses  = NULL           -- completion detected via EndDate, not RunStatus
WHERE id = 1;  -- Purification_Method

-- ── TRIMS: Electrolysis Enrichment ──────────────────────────────────────────
UPDATE dbo.job_procedure SET
    run_table          = 'TRIMS.ElectrolysisRun',
    run_endfld         = 'RunEndTime',
    loadlist_table     = 'TRIMS.Electrolysis',
    loadlist_runfk     = 'RunID',
    loadlist_afield    = 'AnalysisID',
    complete_statuses  = '5'            -- 5 = Enriched
WHERE id = 2;  -- Electrolysis_Enrichment

-- ── TRIMS: LSC ──────────────────────────────────────────────────────────────
UPDATE dbo.job_procedure SET
    run_table          = 'TRIMS.LSCRun',
    run_endfld         = 'RunEndTime',
    loadlist_table     = 'TRIMS.LSCLoadList',
    loadlist_runfk     = 'RunID',
    loadlist_afield    = 'AnalysisID',
    complete_statuses  = '7,8'          -- 7 = Analyzed, 8 = Evaluated
WHERE id = 3;  -- LSC

-- ── SIAM: Stable Isotopes ────────────────────────────────────────────────────
UPDATE dbo.job_procedure SET
    run_table          = 'SIAM.SIAnalysisRun',
    run_endfld         = 'RunEndTime',
    loadlist_table     = 'SIAM.SIAnalysisLoadList',
    loadlist_runfk     = 'SIAnalysisRunID',
    loadlist_afield    = 'AnalysisID',
    complete_statuses  = '7,8'          -- same as TRIMS: 7 = Analyzed, 8 = Evaluated
WHERE id = 5;  -- Stable_Isotopes

-- Chemistry (id=4), Ingrowth (id=10,11), Noble Gas (id=12,13,14):
-- loadlist_table left NULL → trigger skips them gracefully until populated.


-- ============================================================================
-- STEP 3 — Add Analysis.Phase, backfill from old Status codes
-- ============================================================================

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.Analysis') AND name = 'Phase')
    ALTER TABLE dbo.Analysis ADD Phase SMALLINT NOT NULL DEFAULT 0;

-- Backfill: map legacy Status → Phase
UPDATE dbo.Analysis SET Phase = CASE
    WHEN Status = -9                                THEN -1  -- Cancelled
    WHEN Status = 1                                 THEN  0  -- Registered
    WHEN Status = 222                               THEN  1  -- Pending / TBA
    WHEN Status IN (2, 3, 4, 5, 6,
                    29, 98, 99,                              -- Repeat variants → still Ongoing
                    200, 201, 202,
                    203, 204, 205, 206,
                    999)                            THEN  2  -- Ongoing
    WHEN Status IN (7, 210, 921, 922)               THEN  3  -- Analysed  (measurement done, not reviewed)
    WHEN Status IN (8, 211, 923)                    THEN  4  -- Evaluated (QC reviewed)
    WHEN Status IN (9, 220, 221, 924)               THEN  5  -- Reported  (delivered to client)
    ELSE                                                  0  -- fallback: Registered
END;

-- Sample.Status backfill (samples without an Analysis row use Sample.Status for phase)
-- No schema change needed on Sample — the view handles it via COALESCE.


-- ============================================================================
-- STEP 4 — Rebuild vwSampleAnalysisStatus to expose Phase
--
-- CREATE VIEW must be first in a batch → wrapped in EXEC() to run inside
-- the transaction without a GO batch separator.
-- ============================================================================

IF OBJECT_ID('dbo.vwSampleAnalysisStatus', 'V') IS NOT NULL
    DROP VIEW dbo.vwSampleAnalysisStatus;

EXEC(N'
CREATE VIEW dbo.vwSampleAnalysisStatus AS
SELECT
    s.Prefix,
    s.SampleID,
    s.sName                                                     AS SampleName,
    s.MediaID,
    s.Prefix + N''-'' + CAST(s.SampleID AS NVARCHAR(20))       AS OurLabID,
    a.AnalysisID,
    s.SubmissionID,

    -- Legacy status (job-detail codes) — kept for fine-grained display
    COALESCE(a.Status, s.Status)                                AS Status,
    stat.Description,

    -- Legacy columns kept in original positions
    CAST(NULL AS NVARCHAR(255))                                 AS Abbreviation,
    a.Repeats,
    a.PrecursorAnalysisID,

    -- NEW: coarse phase (0–5, -1) — use this for charts and dashboards
    COALESCE(a.Phase,
        CASE
            WHEN s.Status = -9  THEN -1
            WHEN s.Status = 222 THEN  1
            WHEN s.Status = 1   THEN  0
            ELSE                     0
        END
    )                                                           AS Phase,
    pl.Label                                                    AS PhaseLabel

FROM dbo.Sample AS s
INNER JOIN dbo.Submission AS sub
    ON s.SubmissionID = sub.SubmissionID
LEFT OUTER JOIN dbo.Analysis AS a
    ON s.SampleID = a.SampleID AND s.Prefix = a.Prefix
LEFT OUTER JOIN dbo.StatusLookup AS stat
    ON stat.Status = COALESCE(a.Status, s.Status)
LEFT OUTER JOIN dbo.PhaseLookup AS pl
    ON pl.Phase = COALESCE(a.Phase,
        CASE
            WHEN s.Status = -9  THEN -1
            WHEN s.Status = 222 THEN  1
            WHEN s.Status = 1   THEN  0
            ELSE                     0
        END)
WHERE sub.SubmissionType <> 1;
');


-- ============================================================================
-- STEP 5 — usp_RecalculatePhase stored procedure
--
-- Inspects all WorkflowJobs for the analysis''s workflow and checks job
-- completion via the routing data in job_procedure.
-- Called by each trigger wrapper (Step 6).
-- ============================================================================

EXEC(N'
CREATE OR ALTER PROCEDURE dbo.usp_RecalculatePhase
    @AnalysisID INT
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @WorkflowID  SMALLINT;
    DECLARE @Total       INT = 0;
    DECLARE @Complete    INT = 0;
    DECLARE @NewPhase    SMALLINT;
    DECLARE @DynSQL      NVARCHAR(MAX);
    DECLARE @JobDone     BIT;

    -- Cursor variables
    DECLARE @RunTable       NVARCHAR(100),
            @RunEndFld      NVARCHAR(50),
            @LoadListTable  NVARCHAR(100),
            @LoadListRunFK  NVARCHAR(50),
            @LoadListAField NVARCHAR(50),
            @CompleteStatuses NVARCHAR(100),
            @WorkflowJobID  SMALLINT;

    SELECT @WorkflowID = WorkflowID
    FROM dbo.Analysis
    WHERE AnalysisID = @AnalysisID;

    IF @WorkflowID IS NULL RETURN;

    -- Iterate over all routable jobs in the workflow
    DECLARE job_cur CURSOR LOCAL FAST_FORWARD FOR
        SELECT
            jp.run_table,
            jp.run_endfld,
            jp.loadlist_table,
            jp.loadlist_runfk,
            jp.loadlist_afield,
            jp.complete_statuses,
            wj.WorkflowJobID
        FROM dbo.WorkflowJob wj
        JOIN dbo.job_procedure jp ON jp.sname = wj.JobName
        WHERE wj.WorkflowID = @WorkflowID
          AND wj.IsObsolete  = 0
          AND jp.loadlist_table IS NOT NULL;  -- skip Chemistry, Ingrowth, Noble Gas for now

    OPEN job_cur;
    FETCH NEXT FROM job_cur INTO
        @RunTable, @RunEndFld, @LoadListTable,
        @LoadListRunFK, @LoadListAField, @CompleteStatuses, @WorkflowJobID;

    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @Total   = @Total + 1;
        SET @JobDone = 0;

        -- Two modes:
        --   a) RunStatus-based (Electrolysis, LSC, SIAM): RunStatus IN (complete_statuses)
        --   b) EndDate-based   (Distillation):             EndDate IS NOT NULL
        IF @CompleteStatuses IS NOT NULL
        BEGIN
            SET @DynSQL = N''
                SELECT @out = CASE WHEN EXISTS (
                    SELECT 1
                    FROM  '' + @LoadListTable + N'' ll
                    JOIN  '' + @RunTable      + N'' r  ON r.RunID = ll.'' + QUOTENAME(@LoadListRunFK) + N''
                    WHERE ll.'' + QUOTENAME(@LoadListAField) + N'' = @aid
                      AND r.WorkflowJobID = @wjid
                      AND r.RunStatus IN (
                            SELECT TRY_CAST(value AS SMALLINT)
                            FROM STRING_SPLIT(@statuses, N'','')
                          )
                      AND r.'' + QUOTENAME(@RunEndFld) + N'' IS NOT NULL
                ) THEN 1 ELSE 0 END'';

            EXEC sp_executesql
                @DynSQL,
                N''@aid INT, @wjid SMALLINT, @statuses NVARCHAR(100), @out BIT OUTPUT'',
                @aid      = @AnalysisID,
                @wjid     = @WorkflowJobID,
                @statuses = @CompleteStatuses,
                @out      = @JobDone OUTPUT;
        END
        ELSE
        BEGIN
            -- Completion = end-timestamp present (Distillation has no RunStatus)
            SET @DynSQL = N''
                SELECT @out = CASE WHEN EXISTS (
                    SELECT 1
                    FROM  '' + @LoadListTable + N'' ll
                    JOIN  '' + @RunTable      + N'' r  ON r.RunID = ll.'' + QUOTENAME(@LoadListRunFK) + N''
                    WHERE ll.'' + QUOTENAME(@LoadListAField) + N'' = @aid
                      AND r.WorkflowJobID = @wjid
                      AND r.'' + QUOTENAME(@RunEndFld) + N'' IS NOT NULL
                ) THEN 1 ELSE 0 END'';

            EXEC sp_executesql
                @DynSQL,
                N''@aid INT, @wjid SMALLINT, @out BIT OUTPUT'',
                @aid  = @AnalysisID,
                @wjid = @WorkflowJobID,
                @out  = @JobDone OUTPUT;
        END

        IF @JobDone = 1 SET @Complete = @Complete + 1;

        FETCH NEXT FROM job_cur INTO
            @RunTable, @RunEndFld, @LoadListTable,
            @LoadListRunFK, @LoadListAField, @CompleteStatuses, @WorkflowJobID;
    END

    CLOSE     job_cur;
    DEALLOCATE job_cur;

    -- Derive phase from job completion ratio
    SET @NewPhase = CASE
        WHEN @Total   = 0             THEN  0   -- Registered (no jobs in workflow)
        WHEN @Complete = 0            THEN  2   -- Ongoing (jobs exist, none closed)
        WHEN @Complete < @Total       THEN  2   -- Ongoing (partial)
        ELSE                               3   -- Analysed (all jobs closed)
    END;

    -- Never downgrade terminal / manually-set phases
    UPDATE dbo.Analysis
    SET    Phase = @NewPhase
    WHERE  AnalysisID = @AnalysisID
      AND  Phase NOT IN (-1, 4, 5);  -- Cancelled, Evaluated, Reported are terminal
END;
');


-- ============================================================================
-- STEP 6 — Trigger wrappers (one per run table)
--
-- SQL Server triggers fire once per statement, not per row.
-- Each trigger collects affected AnalysisIDs from the load-list table and
-- calls usp_RecalculatePhase for each one.
-- CREATE TRIGGER must be first in a batch → wrapped in EXEC().
-- ============================================================================

-- ── 6a. ElectrolysisRun  (job_procedure id=2, Electrolysis_Enrichment) ───────

IF OBJECT_ID('TRIMS.trg_ElectrolysisRun_PhaseSync', 'TR') IS NOT NULL
    DROP TRIGGER TRIMS.trg_ElectrolysisRun_PhaseSync;

EXEC(N'
CREATE TRIGGER TRIMS.trg_ElectrolysisRun_PhaseSync
ON TRIMS.ElectrolysisRun
AFTER UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    IF NOT (UPDATE(RunStatus) OR UPDATE(RunEndTime)) RETURN;

    -- Only fire when relevant columns actually changed
    IF NOT EXISTS (
        SELECT 1 FROM inserted i JOIN deleted d ON i.RunID = d.RunID
        WHERE i.RunStatus  IS DISTINCT FROM d.RunStatus
           OR i.RunEndTime IS DISTINCT FROM d.RunEndTime
    ) RETURN;

    DECLARE @aid INT;
    DECLARE aid_cur CURSOR LOCAL FAST_FORWARD FOR
        SELECT DISTINCT ll.AnalysisID
        FROM TRIMS.Electrolysis ll
        JOIN inserted i ON i.RunID = ll.RunID;

    OPEN aid_cur;
    FETCH NEXT FROM aid_cur INTO @aid;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        EXEC dbo.usp_RecalculatePhase @aid;
        FETCH NEXT FROM aid_cur INTO @aid;
    END
    CLOSE     aid_cur;
    DEALLOCATE aid_cur;
END;
');


-- ── 6b. LSCRun  (job_procedure id=3, LSC) ───────────────────────────────────

IF OBJECT_ID('TRIMS.trg_LSCRun_PhaseSync', 'TR') IS NOT NULL
    DROP TRIGGER TRIMS.trg_LSCRun_PhaseSync;

EXEC(N'
CREATE TRIGGER TRIMS.trg_LSCRun_PhaseSync
ON TRIMS.LSCRun
AFTER UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    IF NOT (UPDATE(RunStatus) OR UPDATE(RunEndTime)) RETURN;

    IF NOT EXISTS (
        SELECT 1 FROM inserted i JOIN deleted d ON i.RunID = d.RunID
        WHERE i.RunStatus  IS DISTINCT FROM d.RunStatus
           OR i.RunEndTime IS DISTINCT FROM d.RunEndTime
    ) RETURN;

    DECLARE @aid INT;
    DECLARE aid_cur CURSOR LOCAL FAST_FORWARD FOR
        SELECT DISTINCT ll.AnalysisID
        FROM TRIMS.LSCLoadList ll
        JOIN inserted i ON i.RunID = ll.RunID;

    OPEN aid_cur;
    FETCH NEXT FROM aid_cur INTO @aid;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        EXEC dbo.usp_RecalculatePhase @aid;
        FETCH NEXT FROM aid_cur INTO @aid;
    END
    CLOSE     aid_cur;
    DEALLOCATE aid_cur;
END;
');


-- ── 6c. PrimaryDistillationBatch  (job_procedure id=1, Purification_Method) ─
-- No RunStatus — fires when EndDate is set (not previously set).

IF OBJECT_ID('TRIMS.trg_DistillationBatch_PhaseSync', 'TR') IS NOT NULL
    DROP TRIGGER TRIMS.trg_DistillationBatch_PhaseSync;

EXEC(N'
CREATE TRIGGER TRIMS.trg_DistillationBatch_PhaseSync
ON TRIMS.PrimaryDistillationBatch
AFTER UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    IF NOT UPDATE(EndDate) RETURN;

    -- Only fire when EndDate transitions from NULL to a value
    IF NOT EXISTS (
        SELECT 1 FROM inserted i JOIN deleted d ON i.RunID = d.RunID
        WHERE d.EndDate IS NULL AND i.EndDate IS NOT NULL
    ) RETURN;

    DECLARE @aid INT;
    DECLARE aid_cur CURSOR LOCAL FAST_FORWARD FOR
        SELECT DISTINCT ll.AnalysisID
        FROM TRIMS.PrimaryDistillation ll
        JOIN inserted i ON i.RunID = ll.RunID;

    OPEN aid_cur;
    FETCH NEXT FROM aid_cur INTO @aid;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        EXEC dbo.usp_RecalculatePhase @aid;
        FETCH NEXT FROM aid_cur INTO @aid;
    END
    CLOSE     aid_cur;
    DEALLOCATE aid_cur;
END;
');


-- ── 6d. SIAnalysisRun  (job_procedure id=5, Stable_Isotopes) ────────────────

IF OBJECT_ID('SIAM.trg_SIAnalysisRun_PhaseSync', 'TR') IS NOT NULL
    DROP TRIGGER SIAM.trg_SIAnalysisRun_PhaseSync;

EXEC(N'
CREATE TRIGGER SIAM.trg_SIAnalysisRun_PhaseSync
ON SIAM.SIAnalysisRun
AFTER UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    IF NOT (UPDATE(RunStatus) OR UPDATE(RunEndTime)) RETURN;

    IF NOT EXISTS (
        SELECT 1 FROM inserted i JOIN deleted d ON i.SIAnalysisRunID = d.SIAnalysisRunID
        WHERE i.RunStatus  IS DISTINCT FROM d.RunStatus
           OR i.RunEndTime IS DISTINCT FROM d.RunEndTime
    ) RETURN;

    DECLARE @aid INT;
    DECLARE aid_cur CURSOR LOCAL FAST_FORWARD FOR
        SELECT DISTINCT ll.AnalysisID
        FROM SIAM.SIAnalysisLoadList ll
        JOIN inserted i ON i.SIAnalysisRunID = ll.SIAnalysisRunID;

    OPEN aid_cur;
    FETCH NEXT FROM aid_cur INTO @aid;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        EXEC dbo.usp_RecalculatePhase @aid;
        FETCH NEXT FROM aid_cur INTO @aid;
    END
    CLOSE     aid_cur;
    DEALLOCATE aid_cur;
END;
');

-- ── 6e. Future modules (Ingrowth id=10,11 / Noble Gas id=12,13,14) ──────────
-- Add triggers here once their run/loadlist tables are confirmed.
-- usp_RecalculatePhase includes them automatically once job_procedure rows
-- are populated (loadlist_table currently NULL → skipped).


-- ============================================================================
-- STEP 7 — Verification
-- ============================================================================

-- Phase distribution
SELECT
    a.Phase,
    pl.Label,
    COUNT(*) AS N
FROM dbo.Analysis a
LEFT JOIN dbo.PhaseLookup pl ON pl.Phase = a.Phase
GROUP BY a.Phase, pl.Label, pl.SortOrder
ORDER BY pl.SortOrder;

-- Spot-check: any mismatch between expected phase and what was backfilled
SELECT TOP 20
    a.AnalysisID,
    a.Status        AS OldStatus,
    sl.Description  AS OldDescription,
    a.Phase         AS NewPhase,
    pl.Label        AS NewLabel
FROM dbo.Analysis a
LEFT JOIN dbo.StatusLookup sl ON sl.Status = a.Status
LEFT JOIN dbo.PhaseLookup  pl ON pl.Phase  = a.Phase
WHERE
    (a.Status IN (9, 220, 221, 924) AND a.Phase <> 5)   -- Reported → must be Phase 5
    OR
    (a.Status IN (8, 211, 923)      AND a.Phase <> 4)   -- Evaluated → must be Phase 4
    OR
    (a.Status IN (7, 210, 921, 922) AND a.Phase <> 3);  -- Analysed → must be Phase 3

-- Routing completeness check
SELECT
    wj.JobName,
    jp.id,
    jp.sname,
    jp.run_table,
    jp.loadlist_table,
    CASE WHEN jp.loadlist_table IS NULL THEN N'⚠ not routed' ELSE N'✓' END AS Routed
FROM dbo.WorkflowJob wj
LEFT JOIN dbo.job_procedure jp ON jp.sname = wj.JobName
WHERE wj.IsObsolete = 0
ORDER BY wj.JobName;


COMMIT TRANSACTION;

END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
    DECLARE @ErrMsg  NVARCHAR(4000) = ERROR_MESSAGE();
    DECLARE @ErrLine INT            = ERROR_LINE();
    RAISERROR(N'Migration failed at line %d: %s', 16, 1, @ErrLine, @ErrMsg);
END CATCH;

-- =============================================================================
-- DEFERRED — run separately after smoke-test period:
--
--   ALTER TABLE dbo.Analysis DROP COLUMN Status;
--   -- (also remove from vwSampleAnalysisStatus and update all Python queries)
-- =============================================================================
