-- ============================================================
-- Fix submission.Status mismatches against actual sample states.
--
-- Three corrections:
--   A) Advance 222→210/220 when ALL samples have reached that level
--   B) Revert 222→1 when ALL samples are still at Registered (1)
--      and none are in any queue — submission was set to 222 at
--      creation before any queuing happened
--   C) Advance 1→222 when samples have analysis records despite
--      showing status=1 (analyzed via PyQt5 without status update)
--
-- Status codes:
--   1   = Registered  (no samples queued yet)
--   222 = Queued      (samples staged for analysis)
--   210 = Finalized
--   220 = Reported
-- ============================================================

BEGIN;

-- ── A: advance where all samples are done ────────────────────
UPDATE public.submission sub
SET status = agg.new_status
FROM (
    SELECT
        s.submissionid,
        CASE
            WHEN MIN(s.status) >= 220 THEN 220
            WHEN MIN(s.status) >= 210 THEN 210
        END AS new_status
    FROM public.sample s
    WHERE s.sampletype = 0
    GROUP BY s.submissionid
    HAVING MIN(s.status) >= 210
) agg
WHERE sub.submissionid = agg.submissionid
  AND sub.status = 222
  AND agg.new_status IS NOT NULL;

-- ── B: revert where all samples are still registered (1) ─────
-- Only when no sample is in sample_queue or sampletba either
UPDATE public.submission sub
SET status = 1
WHERE sub.status = 222
  AND sub.submissiontype <> 1
  AND NOT EXISTS (
        SELECT 1 FROM public.sample s
        WHERE s.submissionid = sub.submissionid
          AND s.sampletype = 0
          AND s.status <> 1
      )
  AND NOT EXISTS (
        SELECT 1 FROM public.sample s
        JOIN public.sample_queue sq ON sq.sampleid = s.sampleid AND sq.prefix = s.prefix
        WHERE s.submissionid = sub.submissionid
      )
  AND NOT EXISTS (
        SELECT 1 FROM public.sample s
        JOIN public.sampletba tba ON tba.sampleid = s.sampleid AND tba.prefix = s.prefix
        WHERE s.submissionid = sub.submissionid
      );

-- ── C: advance 1→222 where samples already have analysis records ─
UPDATE public.submission sub
SET status = 222
WHERE sub.status = 1
  AND sub.submissiontype <> 1
  AND EXISTS (
        SELECT 1 FROM public.sample s
        JOIN public.analysis a ON a.sampleid=s.sampleid AND a.prefix=s.prefix
        WHERE s.submissionid=sub.submissionid AND s.sampletype=0
      );

COMMIT;
