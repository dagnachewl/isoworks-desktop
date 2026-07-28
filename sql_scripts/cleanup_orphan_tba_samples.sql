-- ============================================================
-- CLEANUP: Remove orphan sampletba rows not in sample_queue
--
-- Targets samples that:
--   • are in public.sampletba (old queue)
--   • are NOT in public.sample_queue (new queue — migration 033)
--   • have NO analysis records
--   • belong to non-reference submissions (submissiontype <> 1)
--
-- Deletion order respects FK constraints:
--   sampletba → sample_queue → dilutiondata → sample_fielddata
--   → sample_duplicate_link → samplearchive → sample → submission
--
-- HOW TO USE:
--   1. Run the PREVIEW block to confirm the target set.
--   2. Run BEGIN … ROLLBACK to do a dry run and check row counts.
--   3. Replace ROLLBACK with COMMIT when satisfied.
-- ============================================================

-- ── STEP 0: PREVIEW ──────────────────────────────────────────
-- Run this block first to see exactly what will be deleted.

SELECT
    s.prefix || '-' || s.sampleid::text AS lab_id,
    s.sampleid,
    s.prefix,
    s.status,
    s.sname                         AS sample_name,
    s.submissionid,
    sub.submissionname              AS project_name,
    sub.submissiontype,
    s.collectiondate                AS sampling_date,
    s.createdatestamp               AS sample_created
FROM public.sample s
INNER JOIN public.sampletba tba
        ON tba.prefix = s.prefix AND tba.sampleid = s.sampleid
LEFT  JOIN public.submission sub
        ON sub.submissionid = s.submissionid
WHERE (sub.submissiontype <> 1 OR sub.submissiontype IS NULL)
  AND NOT EXISTS (
        SELECT 1 FROM public.sample_queue sq
        WHERE sq.sampleid = s.sampleid AND sq.prefix = s.prefix
      )
  AND NOT EXISTS (
        SELECT 1 FROM public.analysis a
        WHERE a.sampleid = s.sampleid AND a.prefix = s.prefix
      )
ORDER BY s.submissionid, s.sampleid;


-- ── STEP 1–N: DELETE (wrapped in a transaction) ──────────────
-- Review the preview above, then run the block below.
-- Replace ROLLBACK with COMMIT when you are happy with the counts.

BEGIN;

-- Capture the target set once so all steps use identical criteria
CREATE TEMP TABLE _orphan_tba ON COMMIT DROP AS
SELECT
    s.sampleid,
    s.prefix,
    s.submissionid
FROM public.sample s
INNER JOIN public.sampletba tba
        ON tba.prefix = s.prefix AND tba.sampleid = s.sampleid
LEFT  JOIN public.submission sub
        ON sub.submissionid = s.submissionid
WHERE (sub.submissiontype <> 1 OR sub.submissiontype IS NULL)
  AND NOT EXISTS (
        SELECT 1 FROM public.sample_queue sq
        WHERE sq.sampleid = s.sampleid AND sq.prefix = s.prefix
      )
  AND NOT EXISTS (
        SELECT 1 FROM public.analysis a
        WHERE a.sampleid = s.sampleid AND a.prefix = s.prefix
      );

-- Summary before we start
SELECT
    COUNT(*)                       AS orphan_samples,
    COUNT(DISTINCT submissionid)   AS affected_submissions
FROM _orphan_tba;

-- 1. sampletba (old queue)
DELETE FROM public.sampletba tba
WHERE EXISTS (
    SELECT 1 FROM _orphan_tba o
    WHERE o.prefix = tba.prefix AND o.sampleid = tba.sampleid
);

-- 2. sample_queue (defensive — should be empty given the NOT EXISTS above)
DELETE FROM public.sample_queue sq
WHERE EXISTS (
    SELECT 1 FROM _orphan_tba o
    WHERE o.prefix = sq.prefix AND o.sampleid = sq.sampleid
);

-- 3. dilutiondata (TRIMS enrichment data)
DELETE FROM public.dilutiondata dd
WHERE EXISTS (
    SELECT 1 FROM _orphan_tba o
    WHERE o.prefix = dd.prefix AND o.sampleid = dd.sampleid
);

-- 4. sample_fielddata (EC, pH, temperature, alkalinity etc.)
DELETE FROM public.sample_fielddata fd
WHERE EXISTS (
    SELECT 1 FROM _orphan_tba o
    WHERE o.prefix = fd.prefix AND o.sampleid = fd.sampleid
);

-- 5. sample_duplicate_link (parent OR duplicate side)
DELETE FROM public.sample_duplicate_link lnk
WHERE EXISTS (
    SELECT 1 FROM _orphan_tba o
    WHERE (o.prefix = lnk.parent_prefix    AND o.sampleid = lnk.parent_sampleid)
       OR (o.prefix = lnk.duplicate_prefix AND o.sampleid = lnk.duplicate_sampleid)
);

-- 6. samplearchive
DELETE FROM public.samplearchive sa
WHERE EXISTS (
    SELECT 1 FROM _orphan_tba o
    WHERE o.prefix = sa.prefix AND o.sampleid = sa.sampleid
);

-- 7. sample itself
DELETE FROM public.sample s
WHERE EXISTS (
    SELECT 1 FROM _orphan_tba o
    WHERE o.prefix = s.prefix AND o.sampleid = s.sampleid
);

-- 8. reporting records for affected empty submissions
DELETE FROM public.reporting r
WHERE r.submissionid IN (SELECT DISTINCT submissionid FROM _orphan_tba)
  AND NOT EXISTS (
        SELECT 1 FROM public.sample s
        WHERE s.submissionid = r.submissionid
      );

-- 9. invoice records for affected empty submissions
DELETE FROM public.invoice inv
WHERE inv.submissionid IN (SELECT DISTINCT submissionid FROM _orphan_tba)
  AND NOT EXISTS (
        SELECT 1 FROM public.sample s
        WHERE s.submissionid = inv.submissionid
      );

-- 10. submissions that are now completely empty
--    (only removes a submission if ALL its samples were just deleted)
DELETE FROM public.submission sub
WHERE sub.submissionid IN (SELECT DISTINCT submissionid FROM _orphan_tba)
  AND NOT EXISTS (
        SELECT 1 FROM public.sample s
        WHERE s.submissionid = sub.submissionid
      );

-- ── Final count check ────────────────────────────────────────
SELECT
    (SELECT COUNT(*) FROM public.sampletba)   AS remaining_tba_rows,
    (SELECT COUNT(*) FROM public.sample_queue) AS sample_queue_rows;

-- ── Confirm or abort ─────────────────────────────────────────
-- If everything looks right, replace the line below with COMMIT;
-- ROLLBACK;
COMMIT;
