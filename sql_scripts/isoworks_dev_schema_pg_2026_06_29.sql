--
-- PostgreSQL database dump
--

\restrict aMM6NP5T7tr7JxTu9hSS1Ck8IRSNUHwAh2NrnuhtUYSaQAFGyAT4TpaSqG5oIG8

-- Dumped from database version 18.2 (Homebrew)
-- Dumped by pg_dump version 18.2 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: ams; Type: SCHEMA; Schema: -; Owner: postgres
--

CREATE SCHEMA ams;


ALTER SCHEMA ams OWNER TO postgres;

--
-- Name: audit; Type: SCHEMA; Schema: -; Owner: postgres
--

CREATE SCHEMA audit;


ALTER SCHEMA audit OWNER TO postgres;

--
-- Name: chem; Type: SCHEMA; Schema: -; Owner: postgres
--

CREATE SCHEMA chem;


ALTER SCHEMA chem OWNER TO postgres;

--
-- Name: ngam; Type: SCHEMA; Schema: -; Owner: postgres
--

CREATE SCHEMA ngam;


ALTER SCHEMA ngam OWNER TO postgres;

--
-- Name: siam; Type: SCHEMA; Schema: -; Owner: postgres
--

CREATE SCHEMA siam;


ALTER SCHEMA siam OWNER TO postgres;

--
-- Name: trims; Type: SCHEMA; Schema: -; Owner: postgres
--

CREATE SCHEMA trims;


ALTER SCHEMA trims OWNER TO postgres;

--
-- Name: uuid-ossp; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;


--
-- Name: EXTENSION "uuid-ossp"; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION "uuid-ossp" IS 'generate universally unique identifiers (UUIDs)';


--
-- Name: audit_table(text, text); Type: FUNCTION; Schema: audit; Owner: postgres
--

CREATE FUNCTION audit.audit_table(p_schema text, p_table text) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_trigger TEXT := 'trg_audit_' || p_table;
    v_full    TEXT := p_schema || '.' || p_table;
BEGIN
    EXECUTE format(
        'DROP TRIGGER IF EXISTS %I ON %s',
        v_trigger, v_full
    );
    EXECUTE format(
        'CREATE TRIGGER %I
         AFTER INSERT OR UPDATE OR DELETE ON %s
         FOR EACH ROW EXECUTE FUNCTION audit.if_modified_func()',
        v_trigger, v_full
    );
END;
$$;


ALTER FUNCTION audit.audit_table(p_schema text, p_table text) OWNER TO postgres;

--
-- Name: if_modified_func(); Type: FUNCTION; Schema: audit; Owner: postgres
--

CREATE FUNCTION audit.if_modified_func() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
DECLARE
    v_old      JSONB;
    v_new      JSONB;
    v_changed  TEXT[];
    v_key      TEXT;
BEGIN
    -- Capture row images
    IF TG_OP = 'DELETE' THEN
        v_old := row_to_json(OLD)::JSONB;
        v_new := NULL;
    ELSIF TG_OP = 'INSERT' THEN
        v_old := NULL;
        v_new := row_to_json(NEW)::JSONB;
    ELSE -- UPDATE
        v_old := row_to_json(OLD)::JSONB;
        v_new := row_to_json(NEW)::JSONB;
        -- Collect only fields whose value actually changed
        SELECT array_agg(k ORDER BY k)
          INTO v_changed
          FROM jsonb_each(v_old) AS x(k, v)
         WHERE v_old->k IS DISTINCT FROM v_new->k;
        -- Skip no-op updates (timestamps drifting, etc.)
        IF v_changed IS NULL THEN
            RETURN NULL;
        END IF;
    END IF;

    INSERT INTO audit.logged_actions (
        schema_name,
        table_name,
        operation,
        changed_at,
        app_user,
        db_user,
        old_data,
        new_data,
        changed_fields
    ) VALUES (
        TG_TABLE_SCHEMA,
        TG_TABLE_NAME,
        TG_OP,
        clock_timestamp(),
        current_setting('app.current_user', true),   -- set by pyLIMS at connection checkout
        current_user,
        v_old,
        v_new,
        v_changed
    );

    RETURN NULL;   -- AFTER trigger; return value is ignored
END;
$$;


ALTER FUNCTION audit.if_modified_func() OWNER TO postgres;

--
-- Name: fn_check_lsc_acceptance(integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.fn_check_lsc_acceptance(p_run_id integer) RETURNS TABLE(analysisid integer, labid text, samplename text, n_done integer, n_expected integer, repeat_status text, metric_name text, metric_value double precision, threshold_pct double precision, qc_decision smallint)
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_proc_id   INTEGER;
    v_threshold DOUBLE PRECISION := 5.0;
BEGIN
    SELECT procedureid INTO v_proc_id
    FROM   trims.lscrun WHERE runid = p_run_id;

    SELECT COALESCE(apm.repeatacceptancepercent, 5)
    INTO   v_threshold
    FROM   public.analysisprocedure_measurable apm
    WHERE  apm.procedureid = v_proc_id
    LIMIT  1;

    RETURN QUERY
    WITH run_analyses AS (
        SELECT DISTINCT ll.analysisid
        FROM   trims.lscloadlist ll
        WHERE  ll.runid = p_run_id
          AND  COALESCE(ll.isignored, FALSE) = FALSE
          AND  ll.sampletype = 0              -- unknowns only
    ),
    expected AS (
        SELECT a.analysisid AS aid,
               GREATEST(COALESCE(a.repeats, 1), 1) AS n_expected
        FROM   public.analysis a
        WHERE  a.analysisid IN (SELECT ra.analysisid FROM run_analyses ra)
    ),
    all_results AS (
        SELECT ll.analysisid AS aid,
               lr.finalactivity AS fvalue
        FROM   trims.lscloadlist ll
        JOIN   trims.lscresult   lr ON lr.analysisid = ll.analysisid
                                   AND lr.runid      = ll.runid
        WHERE  ll.analysisid IN (SELECT ra.analysisid FROM run_analyses ra)
          AND  COALESCE(ll.isignored, FALSE) = FALSE
          AND  lr.finalactivity IS NOT NULL
    ),
    agg AS (
        SELECT  ar.aid,
                COUNT(*)::INTEGER AS n_done,
                CASE WHEN COUNT(*) >= 2 AND ABS(AVG(ar.fvalue)) > 0
                     THEN (MAX(ar.fvalue)-MIN(ar.fvalue))/ABS(AVG(ar.fvalue))*100.0
                     ELSE NULL END AS rpd
        FROM    all_results ar
        GROUP BY ar.aid
    ),
    outcome AS (
        SELECT
            e.aid,
            COALESCE(agg.n_done, 0)  AS n_done,
            e.n_expected,
            CASE WHEN COALESCE(agg.n_done,0) >= e.n_expected
                 THEN 'DONE' ELSE 'PENDING' END                AS rstat,
            CASE WHEN COALESCE(agg.n_done,0) >= 2
                 THEN 'RPD' ELSE 'SingleValue' END             AS mname,
            agg.rpd                                            AS mvalue,
            CASE
                WHEN COALESCE(agg.n_done,0) < e.n_expected THEN 0
                WHEN e.n_expected = 1                       THEN 1
                WHEN COALESCE(agg.n_done,0) < 2            THEN 1
                WHEN agg.rpd IS NOT NULL AND agg.rpd <= v_threshold THEN 1
                WHEN agg.rpd IS NOT NULL                   THEN 2
                ELSE 0
            END::SMALLINT                                      AS qcd
        FROM expected e LEFT JOIN agg ON agg.aid = e.aid
    )
    SELECT o.aid, (s.prefix || '-' || s.sampleid::TEXT)::TEXT, s.sname::TEXT,
           o.n_done, o.n_expected, o.rstat, o.mname, o.mvalue,
           v_threshold, o.qcd
    FROM   outcome o
    LEFT JOIN public.analysis a ON a.analysisid = o.aid
    LEFT JOIN public.sample   s ON s.sampleid   = a.sampleid
                               AND s.prefix      = a.prefix;

    UPDATE trims.lscloadlist ll
    SET    repeat_status = sub.rstat,
           qc_decision   = CASE
               WHEN ll.qc_decision IN (3,4,-1) THEN ll.qc_decision
               ELSE sub.qcd END
    FROM (
        SELECT e2.analysisid,
               CASE WHEN COALESCE(agg2.n_done,0) >= e2.n_expected
                    THEN 'DONE' ELSE 'PENDING' END AS rstat,
               CASE WHEN COALESCE(agg2.n_done,0) < e2.n_expected THEN 0
                    WHEN e2.n_expected = 1                        THEN 1
                    WHEN COALESCE(agg2.n_done,0) < 2             THEN 1
                    WHEN agg2.rpd IS NOT NULL AND agg2.rpd <= v_threshold THEN 1
                    WHEN agg2.rpd IS NOT NULL THEN 2 ELSE 0
               END::SMALLINT AS qcd
        FROM (
            SELECT a2.analysisid, GREATEST(COALESCE(a2.repeats,1),1) AS n_expected
            FROM   public.analysis a2
            WHERE  a2.analysisid IN (
                SELECT DISTINCT ll2.analysisid FROM trims.lscloadlist ll2
                WHERE ll2.runid = p_run_id
                  AND COALESCE(ll2.isignored,FALSE) = FALSE
                  AND ll2.sampletype = 0
            )
        ) e2
        LEFT JOIN (
            SELECT ll3.analysisid,
                   COUNT(*)::INTEGER AS n_done,
                   CASE WHEN COUNT(*) >= 2 AND ABS(AVG(lr2.finalactivity)) > 0
                        THEN (MAX(lr2.finalactivity)-MIN(lr2.finalactivity))
                             / ABS(AVG(lr2.finalactivity)) * 100.0
                        ELSE NULL END AS rpd
            FROM   trims.lscloadlist ll3
            JOIN   trims.lscresult   lr2 ON lr2.analysisid = ll3.analysisid
                                        AND lr2.runid      = ll3.runid
            WHERE  COALESCE(ll3.isignored,FALSE) = FALSE
              AND  lr2.finalactivity IS NOT NULL
            GROUP BY ll3.analysisid
        ) agg2 ON agg2.analysisid = e2.analysisid
    ) sub
    WHERE ll.runid = p_run_id AND ll.analysisid = sub.analysisid
      AND COALESCE(ll.isignored,FALSE) = FALSE;
END;
$$;


ALTER FUNCTION public.fn_check_lsc_acceptance(p_run_id integer) OWNER TO postgres;

--
-- Name: fn_check_ngam_acceptance(integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.fn_check_ngam_acceptance(p_run_id integer) RETURNS TABLE(analysisid integer, labid text, samplename text, n_done integer, n_expected integer, repeat_status text, metric_name text, metric_value double precision, threshold_pct double precision, qc_decision smallint)
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_proc_id   INTEGER;
    v_threshold DOUBLE PRECISION := 5.0;
BEGIN
    SELECT procedureid INTO v_proc_id
    FROM   ngam.ng3hesequencerun WHERE runid = p_run_id;

    SELECT COALESCE(apm.repeatacceptancepercent, 5)
    INTO   v_threshold
    FROM   public.analysisprocedure_measurable apm
    WHERE  apm.procedureid = v_proc_id
    LIMIT  1;

    RETURN QUERY
    WITH run_analyses AS (
        SELECT DISTINCT ll.analysisid
        FROM   ngam.ng3hesequenceloadlist ll
        WHERE  ll.runid    = p_run_id
          AND  ll.analysisid IS NOT NULL
          AND  ll.sampletype = 0            -- unknowns only
          AND  COALESCE(ll.isrejected, FALSE) = FALSE
    ),
    expected AS (
        SELECT a.analysisid AS aid,
               GREATEST(COALESCE(a.repeats, 1), 1) AS n_expected
        FROM   public.analysis a
        WHERE  a.analysisid IN (SELECT ra.analysisid FROM run_analyses ra)
    ),
    all_results AS (
        SELECT ll.analysisid AS aid,
               res.activity_corrected AS fvalue
        FROM   ngam.ng3hesequenceloadlist  ll
        JOIN   ngam.ng3hesequenceresults   res ON res.runid         = ll.runid
                                              AND res.positioninrun = ll.positioninrun
        WHERE  ll.analysisid IN (SELECT ra.analysisid FROM run_analyses ra)
          AND  COALESCE(ll.isrejected,  FALSE) = FALSE
          AND  COALESCE(res.isrejected, FALSE) = FALSE
          AND  res.activity_corrected IS NOT NULL
    ),
    agg AS (
        SELECT  ar.aid,
                COUNT(*)::INTEGER AS n_done,
                CASE WHEN COUNT(*) >= 2 AND ABS(AVG(ar.fvalue)) > 0
                     THEN (MAX(ar.fvalue)-MIN(ar.fvalue))/ABS(AVG(ar.fvalue))*100.0
                     ELSE NULL END AS rpd
        FROM    all_results ar
        GROUP BY ar.aid
    ),
    outcome AS (
        SELECT
            e.aid,
            COALESCE(agg.n_done, 0) AS n_done,
            e.n_expected,
            CASE WHEN COALESCE(agg.n_done,0) >= e.n_expected
                 THEN 'DONE' ELSE 'PENDING' END                AS rstat,
            CASE WHEN COALESCE(agg.n_done,0) >= 2
                 THEN 'RPD' ELSE 'SingleValue' END             AS mname,
            agg.rpd                                            AS mvalue,
            CASE
                WHEN COALESCE(agg.n_done,0) < e.n_expected THEN 0
                WHEN e.n_expected = 1                       THEN 1
                WHEN COALESCE(agg.n_done,0) < 2            THEN 1
                WHEN agg.rpd IS NOT NULL AND agg.rpd <= v_threshold THEN 1
                WHEN agg.rpd IS NOT NULL                   THEN 2
                ELSE 0
            END::SMALLINT                                      AS qcd
        FROM expected e LEFT JOIN agg ON agg.aid = e.aid
    )
    SELECT o.aid, (s.prefix || '-' || s.sampleid::TEXT)::TEXT, s.sname::TEXT,
           o.n_done, o.n_expected, o.rstat, o.mname, o.mvalue,
           v_threshold, o.qcd
    FROM   outcome o
    LEFT JOIN public.analysis a ON a.analysisid = o.aid
    LEFT JOIN public.sample   s ON s.sampleid   = a.sampleid
                               AND s.prefix      = a.prefix;

    UPDATE ngam.ng3hesequenceloadlist ll
    SET    repeat_status = sub.rstat,
           qc_decision   = CASE
               WHEN ll.qc_decision IN (3,4,-1) THEN ll.qc_decision
               ELSE sub.qcd END
    FROM (
        SELECT e2.analysisid,
               CASE WHEN COALESCE(agg2.n_done,0) >= e2.n_expected
                    THEN 'DONE' ELSE 'PENDING' END AS rstat,
               CASE WHEN COALESCE(agg2.n_done,0) < e2.n_expected THEN 0
                    WHEN e2.n_expected = 1                        THEN 1
                    WHEN COALESCE(agg2.n_done,0) < 2             THEN 1
                    WHEN agg2.rpd IS NOT NULL AND agg2.rpd <= v_threshold THEN 1
                    WHEN agg2.rpd IS NOT NULL THEN 2 ELSE 0
               END::SMALLINT AS qcd
        FROM (
            SELECT a2.analysisid, GREATEST(COALESCE(a2.repeats,1),1) AS n_expected
            FROM   public.analysis a2
            WHERE  a2.analysisid IN (
                SELECT DISTINCT ll2.analysisid FROM ngam.ng3hesequenceloadlist ll2
                WHERE ll2.runid = p_run_id AND ll2.analysisid IS NOT NULL
                  AND ll2.sampletype = 0
                  AND COALESCE(ll2.isrejected,FALSE) = FALSE
            )
        ) e2
        LEFT JOIN (
            SELECT ll3.analysisid,
                   COUNT(*)::INTEGER AS n_done,
                   CASE WHEN COUNT(*) >= 2 AND ABS(AVG(res2.activity_corrected)) > 0
                        THEN (MAX(res2.activity_corrected)-MIN(res2.activity_corrected))
                             / ABS(AVG(res2.activity_corrected)) * 100.0
                        ELSE NULL END AS rpd
            FROM   ngam.ng3hesequenceloadlist ll3
            JOIN   ngam.ng3hesequenceresults  res2 ON res2.runid        = ll3.runid
                                                  AND res2.positioninrun = ll3.positioninrun
            WHERE  COALESCE(ll3.isrejected,FALSE)  = FALSE
              AND  COALESCE(res2.isrejected,FALSE)  = FALSE
              AND  res2.activity_corrected IS NOT NULL
            GROUP BY ll3.analysisid
        ) agg2 ON agg2.analysisid = e2.analysisid
    ) sub
    WHERE ll.runid = p_run_id AND ll.analysisid = sub.analysisid
      AND COALESCE(ll.isrejected,FALSE) = FALSE;
END;
$$;


ALTER FUNCTION public.fn_check_ngam_acceptance(p_run_id integer) OWNER TO postgres;

--
-- Name: fn_check_siam_acceptance(integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.fn_check_siam_acceptance(p_run_id integer) RETURNS TABLE(analysisid integer, measurableid integer, labid text, samplename text, parameterlabel text, n_done integer, n_expected integer, repeat_status text, metric_name text, metric_value double precision, threshold_pct double precision, qc_decision smallint)
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_proc_id   INTEGER;
    v_threshold DOUBLE PRECISION := 5.0;
BEGIN
    SELECT procedureid INTO v_proc_id
    FROM siam.sianalysisrun
    WHERE sianalysisrunid = p_run_id;

    SELECT COALESCE(apm.repeatacceptancepercent, 5)
    INTO   v_threshold
    FROM   public.analysisprocedure_measurable apm
    WHERE  apm.procedureid = v_proc_id
    LIMIT  1;

    -- ── Per-analysis × per-measurable results ────────────────────────────
    RETURN QUERY
    WITH run_analyses AS (
        SELECT DISTINCT ll.analysisid
        FROM   siam.sianalysisloadlist ll
        JOIN   public.analysis         ax ON ax.analysisid = ll.analysisid
        WHERE  ll.sianalysisrunid = p_run_id AND ax.status != -1
    ),
    expected AS (
        SELECT a.analysisid AS aid,
               GREATEST(COALESCE(a.repeats, 1), 1) AS n_expected
        FROM   public.analysis a
        WHERE  a.analysisid IN (SELECT ra.analysisid FROM run_analyses ra)
    ),
    -- Count DISTINCT measurement events in THIS run only
    done_counts AS (
        SELECT ll.analysisid AS aid,
               COUNT(DISTINCT ll.sianalysisid)::INTEGER AS n_done
        FROM   siam.sianalysisloadlist ll
        WHERE  ll.sianalysisrunid = p_run_id
          AND  ll.analysisid IN (SELECT ra.analysisid FROM run_analyses ra)
          AND  COALESCE(ll.isignored, FALSE) = FALSE
        GROUP BY ll.analysisid
    ),
    -- Results from THIS run only (RPD is within-run reproducibility)
    all_results AS (
        SELECT ll.analysisid AS aid,
               res.measurableid,
               res.fvalue
        FROM   siam.sianalysisloadlist ll
        JOIN   siam.sianalysisresult   res ON res.sianalysisid = ll.sianalysisid
        WHERE  ll.sianalysisrunid = p_run_id
          AND  ll.analysisid IN (SELECT ra.analysisid FROM run_analyses ra)
          AND  COALESCE(ll.isignored,  FALSE) = FALSE
          AND  COALESCE(res.isignored, FALSE) = FALSE
          AND  res.fvalue IS NOT NULL
    ),
    -- RPD computed per (analysis, measurable)
    agg AS (
        SELECT  ar.aid,
                ar.measurableid,
                CASE WHEN COUNT(*) >= 2 AND ABS(AVG(ar.fvalue)) > 0
                     THEN (MAX(ar.fvalue) - MIN(ar.fvalue)) / ABS(AVG(ar.fvalue)) * 100.0
                     ELSE NULL END AS rpd
        FROM    all_results ar
        GROUP BY ar.aid, ar.measurableid
    ),
    outcome AS (
        SELECT  e.aid,
                agg.measurableid,
                COALESCE(dc.n_done, 0)   AS n_done,
                e.n_expected,
                CASE WHEN COALESCE(dc.n_done, 0) >= e.n_expected
                     THEN 'DONE' ELSE 'PENDING' END                AS rstat,
                CASE WHEN COALESCE(dc.n_done, 0) >= 2
                     THEN 'RPD' ELSE 'SingleValue' END             AS mname,
                agg.rpd                                            AS mvalue,
                CASE
                    WHEN COALESCE(dc.n_done, 0) < e.n_expected    THEN 0
                    WHEN e.n_expected = 1                          THEN 1
                    WHEN COALESCE(dc.n_done, 0) < 2               THEN 1
                    WHEN agg.rpd IS NOT NULL AND agg.rpd <= v_threshold THEN 1
                    WHEN agg.rpd IS NOT NULL                       THEN 2
                    ELSE 0
                END::SMALLINT                                       AS qcd
        FROM expected e
        JOIN agg         ON agg.aid  = e.aid
        LEFT JOIN done_counts dc ON dc.aid = e.aid
    )
    SELECT o.aid, o.measurableid::INTEGER,
           (s.prefix || '-' || s.sampleid::TEXT)::TEXT,
           s.sname::TEXT,
           m.parameterlabel::TEXT,
           o.n_done, o.n_expected, o.rstat, o.mname, o.mvalue,
           v_threshold, o.qcd
    FROM outcome o
    JOIN public.measurables m   ON m.measurableid = o.measurableid
    LEFT JOIN public.analysis a ON a.analysisid   = o.aid
    LEFT JOIN public.sample   s ON s.sampleid     = a.sampleid
                               AND s.prefix        = a.prefix
    ORDER BY o.aid, o.measurableid;

    -- ── Update loadlist: worst-case qc_decision across all measurables ────
    UPDATE siam.sianalysisloadlist ll
    SET    repeat_status = sub.rstat,
           qc_decision   = CASE
               WHEN ll.qc_decision IN (3, 4, -1) THEN ll.qc_decision
               ELSE sub.worst_qcd
           END
    FROM (
        SELECT  e2.analysisid,
                CASE WHEN COALESCE(dc2.n_done, 0) >= e2.n_expected
                     THEN 'DONE' ELSE 'PENDING' END AS rstat,
                -- FAIL (2) beats PENDING (0) beats PASS (1)
                MAX(CASE
                    WHEN COALESCE(dc2.n_done, 0) < e2.n_expected    THEN 0
                    WHEN e2.n_expected = 1                           THEN 1
                    WHEN COALESCE(dc2.n_done, 0) < 2                THEN 1
                    WHEN agg2.rpd IS NOT NULL AND agg2.rpd <= v_threshold THEN 1
                    WHEN agg2.rpd IS NOT NULL                        THEN 2
                    ELSE 0
                END)::SMALLINT AS worst_qcd
        FROM (
            SELECT a2.analysisid,
                   GREATEST(COALESCE(a2.repeats, 1), 1) AS n_expected
            FROM   public.analysis a2
            WHERE  a2.analysisid IN (
                SELECT DISTINCT ll2.analysisid
                FROM   siam.sianalysisloadlist ll2
                JOIN   public.analysis         ax ON ax.analysisid = ll2.analysisid
                WHERE  ll2.sianalysisrunid = p_run_id AND ax.status != -1
            )
        ) e2
        LEFT JOIN (
            SELECT ll3.analysisid,
                   COUNT(DISTINCT ll3.sianalysisid)::INTEGER AS n_done
            FROM   siam.sianalysisloadlist ll3
            WHERE  ll3.sianalysisrunid = p_run_id
              AND  COALESCE(ll3.isignored, FALSE) = FALSE
            GROUP BY ll3.analysisid
        ) dc2 ON dc2.analysisid = e2.analysisid
        LEFT JOIN (
            SELECT ll4.analysisid,
                   res2.measurableid,
                   CASE WHEN COUNT(*) >= 2 AND ABS(AVG(res2.fvalue)) > 0
                        THEN (MAX(res2.fvalue) - MIN(res2.fvalue)) / ABS(AVG(res2.fvalue)) * 100.0
                        ELSE NULL END AS rpd
            FROM   siam.sianalysisloadlist ll4
            JOIN   siam.sianalysisresult   res2 ON res2.sianalysisid = ll4.sianalysisid
            WHERE  ll4.sianalysisrunid = p_run_id
              AND  COALESCE(ll4.isignored,  FALSE) = FALSE
              AND  COALESCE(res2.isignored, FALSE) = FALSE
              AND  res2.fvalue IS NOT NULL
            GROUP BY ll4.analysisid, res2.measurableid
        ) agg2 ON agg2.analysisid = e2.analysisid
        GROUP BY e2.analysisid, e2.n_expected, dc2.n_done
    ) sub
    WHERE ll.sianalysisrunid = p_run_id
      AND ll.analysisid      = sub.analysisid;

END;
$$;


ALTER FUNCTION public.fn_check_siam_acceptance(p_run_id integer) OWNER TO postgres;

--
-- Name: fn_recalculate_phase(integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.fn_recalculate_phase(p_analysis_id integer) RETURNS void
    LANGUAGE plpgsql
    AS $_$
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
          AND jp.loadlist_table IS NOT NULL
    LOOP
        v_total := v_total + 1;

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
                rec.loadlist_runfk,   -- r.<pk>
                rec.loadlist_runfk,   -- ll.<fk>
                rec.loadlist_afield,
                rec.run_endfld
            );
            EXECUTE dyn_sql
                INTO job_done
                USING p_analysis_id, rec.workflowjobid, rec.complete_statuses;
        ELSE
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
                rec.loadlist_runfk,   -- r.<pk>
                rec.loadlist_runfk,   -- ll.<fk>
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

    v_new_phase := CASE
        WHEN v_total    = 0       THEN  0
        WHEN v_complete = 0       THEN  2
        WHEN v_complete < v_total THEN  2
        ELSE                           3
    END;

    UPDATE public.analysis
    SET    phase = v_new_phase
    WHERE  analysisid = p_analysis_id
      AND  phase NOT IN (-1, 4, 5);
END;
$_$;


ALTER FUNCTION public.fn_recalculate_phase(p_analysis_id integer) OWNER TO postgres;

--
-- Name: sp_stage_forward(integer, integer[], character varying); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.sp_stage_forward(p_current_wjid integer, p_analysisids integer[], p_queued_by character varying DEFAULT 'system'::character varying) RETURNS TABLE(analysisid integer, staged boolean, skip_reason text)
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_next_wjid  INTEGER;
    v_aid        INTEGER;
    v_sampleid   INTEGER;
    v_prefix     VARCHAR(1);
    v_mediaid    INTEGER;
    v_priority   INTEGER;
BEGIN
    -- Resolve the next step once — same for every analysisid in this call
    SELECT wj2.workflowjobid INTO v_next_wjid
    FROM public.workflowjob wj1
    JOIN public.workflowjob wj2
      ON  wj2.workflowid  = wj1.workflowid
      AND wj2.runsequence = wj1.runsequence + 1
      AND COALESCE(wj2.isobsolete, FALSE) = FALSE
    WHERE wj1.workflowjobid = p_current_wjid
    LIMIT 1;

    FOREACH v_aid IN ARRAY p_analysisids LOOP

        -- Resolve sample identity and media
        SELECT a.sampleid, a.prefix, sm.mediaid
        INTO   v_sampleid, v_prefix, v_mediaid
        FROM   public.analysis a
        JOIN   public.sample   sm ON sm.sampleid = a.sampleid AND sm.prefix = a.prefix
        WHERE  a.analysisid = v_aid;

        IF NOT FOUND THEN
            analysisid  := v_aid;
            staged      := FALSE;
            skip_reason := 'analysis record not found';
            RETURN NEXT;
            CONTINUE;
        END IF;

        -- Capture priority from the current queue entry before removing it
        SELECT priorityid INTO v_priority
        FROM   public.sample_queue
        WHERE  sampleid      = v_sampleid
          AND  prefix        = v_prefix
          AND  workflowjobid = p_current_wjid;

        -- Remove from current step's queue (no-op if already absent)
        DELETE FROM public.sample_queue
        WHERE  sampleid      = v_sampleid
          AND  prefix        = v_prefix
          AND  workflowjobid = p_current_wjid;

        IF v_next_wjid IS NULL THEN
            analysisid  := v_aid;
            staged      := FALSE;
            skip_reason := 'no next workflow step';
            RETURN NEXT;
            CONTINUE;
        END IF;

        -- Stage into next step's queue; ON CONFLICT refreshes source_aid in
        -- case of a re-finalize (sample already queued there from a prior call)
        INSERT INTO public.sample_queue
            (sampleid, prefix, workflowjobid, mediaid, source_aid, priorityid, queued_by)
        VALUES
            (v_sampleid, v_prefix, v_next_wjid, v_mediaid, v_aid, v_priority, p_queued_by)
        ON CONFLICT (sampleid, prefix, workflowjobid) DO UPDATE
            SET source_aid = EXCLUDED.source_aid,
                queued_by  = EXCLUDED.queued_by;

        analysisid  := v_aid;
        staged      := TRUE;
        skip_reason := NULL;
        RETURN NEXT;

    END LOOP;
END;
$$;


ALTER FUNCTION public.sp_stage_forward(p_current_wjid integer, p_analysisids integer[], p_queued_by character varying) OWNER TO postgres;

--
-- Name: FUNCTION sp_stage_forward(p_current_wjid integer, p_analysisids integer[], p_queued_by character varying); Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON FUNCTION public.sp_stage_forward(p_current_wjid integer, p_analysisids integer[], p_queued_by character varying) IS 'Advance a set of completed analysisids from one workflow step queue to the next.
Caller is responsible for filtering to eligible analysisids (e.g. excluding
samples already measured at the next step). Returns one row per input analysisid.';


--
-- Name: status_label(smallint, text); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.status_label(p_status smallint, p_module text) RETURNS text
    LANGUAGE sql STABLE
    AS $$
    SELECT COALESCE(
        (SELECT label FROM public.status_label
         WHERE  module = p_module AND status = p_status),
        (SELECT label FROM public.status_label
         WHERE  module = 'GLOBAL'  AND status = p_status),
        (SELECT description FROM public.statuslookup
         WHERE  status = p_status)
    )
$$;


ALTER FUNCTION public.status_label(p_status smallint, p_module text) OWNER TO postgres;

--
-- Name: FUNCTION status_label(p_status smallint, p_module text); Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON FUNCTION public.status_label(p_status smallint, p_module text) IS 'Module-aware label: checks module-specific row, then GLOBAL, then statuslookup (PyQt5 compat fallback)';


--
-- Name: trg_distillationbatch_phase_sync(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.trg_distillationbatch_phase_sync() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM public.fn_recalculate_phase(ll.analysisid)
    FROM trims.primarydistillation ll
    WHERE ll.runid = NEW.runid;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.trg_distillationbatch_phase_sync() OWNER TO postgres;

--
-- Name: trg_electrolysisrun_phase_sync(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.trg_electrolysisrun_phase_sync() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM public.fn_recalculate_phase(ll.analysisid)
    FROM trims.electrolysis ll          -- load list: trims.electrolysis
    WHERE ll.runid = NEW.runid;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.trg_electrolysisrun_phase_sync() OWNER TO postgres;

--
-- Name: trg_lscrun_phase_sync(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.trg_lscrun_phase_sync() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM public.fn_recalculate_phase(ll.analysisid)
    FROM trims.lscloadlist ll
    WHERE ll.runid = NEW.runid;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.trg_lscrun_phase_sync() OWNER TO postgres;

--
-- Name: trg_sianalysisrun_phase_sync(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.trg_sianalysisrun_phase_sync() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM public.fn_recalculate_phase(ll.analysisid)
    FROM siam.sianalysisloadlist ll
    WHERE ll.sianalysisrunid = NEW.sianalysisrunid;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.trg_sianalysisrun_phase_sync() OWNER TO postgres;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: amsmeasurement; Type: TABLE; Schema: ams; Owner: postgres
--

CREATE TABLE ams.amsmeasurement (
    amsmeasurementid integer NOT NULL,
    amstargetid integer NOT NULL,
    cyclenumber smallint NOT NULL,
    ratio14_12 double precision,
    ratio13_12 double precision,
    current12c_ua double precision,
    counts14c integer,
    runtime_s double precision,
    isrejected boolean DEFAULT false NOT NULL
);


ALTER TABLE ams.amsmeasurement OWNER TO postgres;

--
-- Name: TABLE amsmeasurement; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON TABLE ams.amsmeasurement IS 'Per-cycle raw ratio data for a target; optional (aggregate-only files omit this).';


--
-- Name: COLUMN amsmeasurement.isrejected; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.amsmeasurement.isrejected IS 'TRUE when cycle was flagged as outlier during data reduction.';


--
-- Name: amsmeasurement_amsmeasurementid_seq; Type: SEQUENCE; Schema: ams; Owner: postgres
--

ALTER TABLE ams.amsmeasurement ALTER COLUMN amsmeasurementid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ams.amsmeasurement_amsmeasurementid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: amsresult; Type: TABLE; Schema: ams; Owner: postgres
--

CREATE TABLE ams.amsresult (
    amsresultid integer NOT NULL,
    amstargetid integer NOT NULL,
    rawratio14_12 double precision,
    rawratio14_12_err double precision,
    ratio13_12 double precision,
    ratio13_12_err double precision,
    delta13c_permil double precision,
    fm_raw double precision,
    fm_raw_err double precision,
    fm_corrected double precision,
    fm_corrected_err double precision,
    pmc double precision,
    congage_bp double precision,
    congage_err double precision,
    nstd_used smallint,
    nblank_used smallint,
    isaccepted boolean DEFAULT true NOT NULL,
    rejectreason character varying(255),
    reducedat timestamp without time zone,
    reducedby character varying(100)
);


ALTER TABLE ams.amsresult OWNER TO postgres;

--
-- Name: TABLE amsresult; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON TABLE ams.amsresult IS 'Reduced 14C result per target; recomputable from amsmeasurement + amstarget.';


--
-- Name: COLUMN amsresult.fm_corrected; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.amsresult.fm_corrected IS 'Fraction modern after OXI normalisation and δ13C fractionation correction.';


--
-- Name: COLUMN amsresult.congage_bp; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.amsresult.congage_bp IS 'Conventional radiocarbon age = −8033 × ln(fm_corrected); NULL when fm_corrected ≤ 0.';


--
-- Name: COLUMN amsresult.nstd_used; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.amsresult.nstd_used IS 'Count of OXI standard targets on the same wheel used in the normalisation mean.';


--
-- Name: amsresult_amsresultid_seq; Type: SEQUENCE; Schema: ams; Owner: postgres
--

ALTER TABLE ams.amsresult ALTER COLUMN amsresultid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ams.amsresult_amsresultid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: amsrun; Type: TABLE; Schema: ams; Owner: postgres
--

CREATE TABLE ams.amsrun (
    amsrunid integer NOT NULL,
    runcode character varying(50) NOT NULL,
    rundate date,
    equipmentid integer,
    technicianid integer,
    datapath character varying(500),
    notes character varying(500),
    runstatus smallint DEFAULT 0 NOT NULL,
    islocked boolean DEFAULT false NOT NULL,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE ams.amsrun OWNER TO postgres;

--
-- Name: TABLE amsrun; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON TABLE ams.amsrun IS 'AMS analytical run (one per session / batch submission).';


--
-- Name: COLUMN amsrun.runcode; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.amsrun.runcode IS 'Unique run identifier, typically from the instrument file header.';


--
-- Name: COLUMN amsrun.runstatus; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.amsrun.runstatus IS '0=open, 1=reduced, 2=approved, 3=locked.';


--
-- Name: amsrun_amsrunid_seq; Type: SEQUENCE; Schema: ams; Owner: postgres
--

ALTER TABLE ams.amsrun ALTER COLUMN amsrunid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ams.amsrun_amsrunid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: amstarget; Type: TABLE; Schema: ams; Owner: postgres
--

CREATE TABLE ams.amstarget (
    amstargetid integer NOT NULL,
    amswheelid integer NOT NULL,
    wheelposition smallint NOT NULL,
    targetlabel character varying(100) NOT NULL,
    targettype character varying(20) DEFAULT 'unknown'::character varying NOT NULL,
    analysisid integer,
    samplemass_mg numeric(8,4),
    ncycles smallint,
    runtime_s double precision,
    notes character varying(255),
    graphsampleid integer,
    samplevolume_ml double precision,
    CONSTRAINT ck_amstarget_type CHECK (((targettype)::text = ANY ((ARRAY['unknown'::character varying, 'OXI'::character varying, 'OXII'::character varying, 'process_blank'::character varying, 'graphite_blank'::character varying, 'secondary_std'::character varying, 'other'::character varying, 'gas_unknown'::character varying, 'gas_blank'::character varying])::text[])))
);


ALTER TABLE ams.amstarget OWNER TO postgres;

--
-- Name: TABLE amstarget; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON TABLE ams.amstarget IS 'Individual target (vial/graphite cathode) occupying one wheel position.';


--
-- Name: COLUMN amstarget.targettype; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.amstarget.targettype IS 'unknown | OXI | OXII | process_blank | graphite_blank | secondary_std | other | gas_unknown | gas_blank';


--
-- Name: COLUMN amstarget.samplemass_mg; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.amstarget.samplemass_mg IS 'Carbon mass (mg) — required for mass-balance blank correction.';


--
-- Name: COLUMN amstarget.graphsampleid; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.amstarget.graphsampleid IS 'FK to ams.graphsample; NULL for calibration standards and blanks.';


--
-- Name: COLUMN amstarget.samplevolume_ml; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.amstarget.samplevolume_ml IS 'CO₂ volume injected via GIS, in mL at STP. NULL for solid graphite targets.';


--
-- Name: amstarget_amstargetid_seq; Type: SEQUENCE; Schema: ams; Owner: postgres
--

ALTER TABLE ams.amstarget ALTER COLUMN amstargetid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ams.amstarget_amstargetid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: amswheel; Type: TABLE; Schema: ams; Owner: postgres
--

CREATE TABLE ams.amswheel (
    amswheelid integer NOT NULL,
    amsrunid integer NOT NULL,
    wheelnumber smallint DEFAULT 1 NOT NULL,
    wheellabel character varying(50),
    loaddate date,
    datafilepath character varying(500),
    machinename character varying(100),
    operatorname character varying(100),
    notes character varying(255)
);


ALTER TABLE ams.amswheel OWNER TO postgres;

--
-- Name: TABLE amswheel; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON TABLE ams.amswheel IS 'Wheel/magazine within an AMS run; one per imported data file.';


--
-- Name: COLUMN amswheel.wheellabel; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.amswheel.wheellabel IS 'Wheel identifier as reported in the instrument export file.';


--
-- Name: amswheel_amswheelid_seq; Type: SEQUENCE; Schema: ams; Owner: postgres
--

ALTER TABLE ams.amswheel ALTER COLUMN amswheelid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ams.amswheel_amswheelid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: graphrun; Type: TABLE; Schema: ams; Owner: postgres
--

CREATE TABLE ams.graphrun (
    graphrunid integer NOT NULL,
    batchcode character varying(50) NOT NULL,
    batchdate date,
    equipmentid integer,
    technicianid integer,
    notes character varying(500),
    runstatus smallint DEFAULT 0 NOT NULL,
    islocked boolean DEFAULT false NOT NULL,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE ams.graphrun OWNER TO postgres;

--
-- Name: TABLE graphrun; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON TABLE ams.graphrun IS 'Graphitisation batch header (CO2 → graphite conversion session).';


--
-- Name: COLUMN graphrun.batchcode; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.graphrun.batchcode IS 'Unique batch identifier, e.g. "GR240115A".';


--
-- Name: COLUMN graphrun.runstatus; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.graphrun.runstatus IS '0=open, 1=complete, 2=approved, 3=locked.';


--
-- Name: graphrun_graphrunid_seq; Type: SEQUENCE; Schema: ams; Owner: postgres
--

ALTER TABLE ams.graphrun ALTER COLUMN graphrunid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ams.graphrun_graphrunid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: graphsample; Type: TABLE; Schema: ams; Owner: postgres
--

CREATE TABLE ams.graphsample (
    graphsampleid integer NOT NULL,
    graphrunid integer,
    analysisid integer NOT NULL,
    isbypass boolean DEFAULT false NOT NULL,
    batchposition smallint,
    samplemass_mg numeric(8,3),
    fillpressure_mbar numeric(8,2),
    catalysttype character varying(10) DEFAULT 'Fe'::character varying NOT NULL,
    reactortemp_c smallint,
    reductionduration_h numeric(5,2),
    finalpressure_mbar numeric(8,2),
    graphitemass_mg numeric(8,3),
    carbonyield_pct numeric(5,1),
    isaccepted boolean DEFAULT true NOT NULL,
    rejectreason character varying(255),
    notes character varying(500),
    status smallint DEFAULT 0 NOT NULL,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50),
    CONSTRAINT ck_graphsample_catalyst CHECK (((catalysttype)::text = ANY ((ARRAY['Fe'::character varying, 'Co'::character varying, 'Ni'::character varying])::text[]))),
    CONSTRAINT ck_graphsample_status CHECK ((status = ANY (ARRAY[0, 1, 2, 3])))
);


ALTER TABLE ams.graphsample OWNER TO postgres;

--
-- Name: TABLE graphsample; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON TABLE ams.graphsample IS 'Per-sample graphitisation record; also used for bypass (pre-made graphite) via isbypass=TRUE.';


--
-- Name: COLUMN graphsample.graphrunid; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.graphsample.graphrunid IS 'NULL when isbypass=TRUE (sample submitted as finished graphite, no reaction performed here).';


--
-- Name: COLUMN graphsample.isbypass; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.graphsample.isbypass IS 'TRUE = submitted as pre-made graphite; reaction columns will be NULL.';


--
-- Name: COLUMN graphsample.graphitemass_mg; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.graphsample.graphitemass_mg IS 'Final graphite mass (mg) — passed to amstarget.samplemass_mg for mass-balance blank correction.';


--
-- Name: COLUMN graphsample.status; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.graphsample.status IS '0=pending, 1=in_progress, 2=complete, 3=failed.';


--
-- Name: graphsample_graphsampleid_seq; Type: SEQUENCE; Schema: ams; Owner: postgres
--

ALTER TABLE ams.graphsample ALTER COLUMN graphsampleid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ams.graphsample_graphsampleid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: wheeltemplate; Type: TABLE; Schema: ams; Owner: postgres
--

CREATE TABLE ams.wheeltemplate (
    templateid integer NOT NULL,
    templatename character varying(100) NOT NULL,
    npositions smallint DEFAULT 40 NOT NULL,
    layoutjson text NOT NULL,
    notes character varying(255),
    isactive boolean DEFAULT true NOT NULL,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50)
);


ALTER TABLE ams.wheeltemplate OWNER TO postgres;

--
-- Name: TABLE wheeltemplate; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON TABLE ams.wheeltemplate IS 'Saved AMS wheel position templates; layoutjson is a JSON array of {pos, type} objects.';


--
-- Name: COLUMN wheeltemplate.layoutjson; Type: COMMENT; Schema: ams; Owner: postgres
--

COMMENT ON COLUMN ams.wheeltemplate.layoutjson IS 'JSON array: [{\"pos\":1,\"type\":\"OXI\"}, ...]. type must be a valid amstarget targettype value.';


--
-- Name: wheeltemplate_templateid_seq; Type: SEQUENCE; Schema: ams; Owner: postgres
--

ALTER TABLE ams.wheeltemplate ALTER COLUMN templateid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ams.wheeltemplate_templateid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: logged_actions; Type: TABLE; Schema: audit; Owner: postgres
--

CREATE TABLE audit.logged_actions (
    action_id bigint NOT NULL,
    schema_name text NOT NULL,
    table_name text NOT NULL,
    operation text NOT NULL,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    app_user text,
    db_user text,
    old_data jsonb,
    new_data jsonb,
    changed_fields text[]
);


ALTER TABLE audit.logged_actions OWNER TO postgres;

--
-- Name: TABLE logged_actions; Type: COMMENT; Schema: audit; Owner: postgres
--

COMMENT ON TABLE audit.logged_actions IS 'Immutable audit log — do not UPDATE or DELETE rows from this table.';


--
-- Name: logged_actions_action_id_seq; Type: SEQUENCE; Schema: audit; Owner: postgres
--

CREATE SEQUENCE audit.logged_actions_action_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE audit.logged_actions_action_id_seq OWNER TO postgres;

--
-- Name: logged_actions_action_id_seq; Type: SEQUENCE OWNED BY; Schema: audit; Owner: postgres
--

ALTER SEQUENCE audit.logged_actions_action_id_seq OWNED BY audit.logged_actions.action_id;


--
-- Name: v_recent_changes; Type: VIEW; Schema: audit; Owner: postgres
--

CREATE VIEW audit.v_recent_changes AS
 SELECT action_id,
    changed_at,
    COALESCE(app_user, db_user, '?'::text) AS app_user,
    operation,
    ((schema_name || '.'::text) || table_name) AS full_table,
    schema_name,
    table_name,
    COALESCE(array_to_string(changed_fields, ', '::text),
        CASE operation
            WHEN 'INSERT'::text THEN '(new row)'::text
            ELSE '(deleted)'::text
        END) AS summary,
    old_data,
    new_data,
    changed_fields
   FROM audit.logged_actions
  ORDER BY changed_at DESC;


ALTER VIEW audit.v_recent_changes OWNER TO postgres;

--
-- Name: batch; Type: TABLE; Schema: chem; Owner: postgres
--

CREATE TABLE chem.batch (
    runid integer CONSTRAINT chemistrybatch_runid_not_null NOT NULL,
    workflowid smallint,
    workflowjobid smallint,
    procedureid smallint,
    equipmentid smallint,
    datapath character varying(100),
    headerstoimport character varying(255),
    defaultmeasurableid integer,
    islocked boolean,
    technicianid integer,
    runstatus integer,
    runstarttime timestamp without time zone,
    runendtime timestamp without time zone,
    remarks character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50)
);


ALTER TABLE chem.batch OWNER TO postgres;

--
-- Name: chemistrybatch_runid_seq; Type: SEQUENCE; Schema: chem; Owner: postgres
--

ALTER TABLE chem.batch ALTER COLUMN runid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME chem.chemistrybatch_runid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: loadlist; Type: TABLE; Schema: chem; Owner: postgres
--

CREATE TABLE chem.loadlist (
    chemanalysisid integer CONSTRAINT chemistryloadlist_chemanalysisid_not_null NOT NULL,
    runid integer DEFAULT 0 CONSTRAINT chemistryloadlist_runid_not_null NOT NULL,
    positionno smallint DEFAULT 0 CONSTRAINT chemistryloadlist_positionno_not_null NOT NULL,
    analysisid integer,
    analysisnumber integer,
    status smallint,
    repeat smallint,
    measurableid integer,
    fieldvalue double precision,
    measuredvalue double precision,
    passrequirement boolean,
    remarks character varying(100)
);


ALTER TABLE chem.loadlist OWNER TO postgres;

--
-- Name: chemistryloadlist_chemanalysisid_seq; Type: SEQUENCE; Schema: chem; Owner: postgres
--

ALTER TABLE chem.loadlist ALTER COLUMN chemanalysisid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME chem.chemistryloadlist_chemanalysisid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: public_chemistryprocedure_procedureid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_chemistryprocedure_procedureid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_chemistryprocedure_procedureid_seq OWNER TO postgres;

--
-- Name: chemproc; Type: TABLE; Schema: chem; Owner: postgres
--

CREATE TABLE chem.chemproc (
    procedureid smallint DEFAULT nextval('public.public_chemistryprocedure_procedureid_seq'::regclass) CONSTRAINT chemistryprocedure_procedureid_not_null NOT NULL,
    procedurename character varying(255),
    maxposition smallint,
    fileformatloadlist character varying(255),
    fileformatdata character varying(255),
    samplevolume double precision,
    listcations character varying(255),
    listanions character varying(255),
    loadliststring character varying(80),
    reportingtextmemo text,
    remarks character varying(255),
    isobsolete boolean DEFAULT false CONSTRAINT chemistryprocedure_isobsolete_not_null NOT NULL,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE chem.chemproc OWNER TO postgres;

--
-- Name: public_chemistrydata_chemanalysisid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_chemistrydata_chemanalysisid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_chemistrydata_chemanalysisid_seq OWNER TO postgres;

--
-- Name: data; Type: TABLE; Schema: chem; Owner: postgres
--

CREATE TABLE chem.data (
    chemanalysisid integer DEFAULT nextval('public.public_chemistrydata_chemanalysisid_seq'::regclass) CONSTRAINT chemistrydata_chemanalysisid_not_null NOT NULL,
    measurableid integer DEFAULT 0 CONSTRAINT chemistrydata_measurableid_not_null NOT NULL,
    repeat smallint DEFAULT 0 CONSTRAINT chemistrydata_repeat_not_null NOT NULL,
    measurableunit smallint,
    fvalue double precision,
    fvalueunc double precision,
    qualifier character varying(255),
    qcflag character varying(255),
    "precision" double precision,
    detectionlimit double precision,
    remarks character varying(255)
);


ALTER TABLE chem.data OWNER TO postgres;

--
-- Name: msrun; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.msrun (
    runid integer NOT NULL,
    measurement_mode character varying(2) NOT NULL,
    workflowid smallint,
    equipmentid integer,
    procedureid integer,
    technicianid integer,
    runstarttime timestamp without time zone,
    runendtime timestamp without time zone,
    runstatus smallint,
    islocked boolean DEFAULT false NOT NULL,
    remarks character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50),
    workflowjobid smallint,
    datapath character varying(255),
    headerstoimport character varying(255),
    storedheaders character varying(255),
    minutescompleted integer,
    meanbackground double precision,
    meanbackgroundunc double precision,
    meanstandard double precision,
    meanstandardunc double precision,
    outliermethod character varying(25),
    nvcprotocolfilepath character varying(255),
    nvcstatusfilepath character varying(255),
    bg_proxy_mode character varying(20) DEFAULT 'auto'::character varying,
    bg_proxy_factor_4he double precision DEFAULT 100.0,
    CONSTRAINT msrun_measurement_mode_check CHECK (((measurement_mode)::text = ANY ((ARRAY['NG'::character varying, 'IG'::character varying])::text[])))
);


ALTER TABLE ngam.msrun OWNER TO postgres;

--
-- Name: ng3hesequenceloadlist; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ng3hesequenceloadlist (
    headerid integer NOT NULL,
    runid integer DEFAULT 0 NOT NULL,
    analysisid integer DEFAULT 0 NOT NULL,
    ingrowthid integer,
    precursorid integer,
    positioninrun smallint DEFAULT 0 NOT NULL,
    traynumber character varying(5),
    positionintray smallint,
    sampletype smallint DEFAULT 0 NOT NULL,
    sampleamount real,
    measurementtime double precision,
    status smallint,
    knownstdactivity double precision,
    knownstdactivityunc double precision,
    isrejected boolean DEFAULT false NOT NULL,
    remarks character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    bisblank boolean DEFAULT false NOT NULL,
    bisreproreference boolean DEFAULT false NOT NULL,
    bislinreference boolean DEFAULT false NOT NULL,
    knownstdactivityunitid integer,
    qc_decision smallint DEFAULT 0 NOT NULL,
    repeat_status character varying(10) DEFAULT 'PENDING'::character varying NOT NULL,
    inlet_event smallint,
    inlet_note text
);


ALTER TABLE ngam.ng3hesequenceloadlist OWNER TO postgres;

--
-- Name: ngpreparations; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngpreparations (
    inoblepreparationid integer NOT NULL,
    runid integer,
    positioninrun integer,
    nvcinletstring character varying(100),
    analysisid integer,
    iportnumber integer,
    status integer,
    bisblank boolean DEFAULT false NOT NULL,
    bisreproreference boolean DEFAULT false NOT NULL,
    bislinreference boolean DEFAULT false NOT NULL,
    nvcreferencegas character varying(50),
    freferenceamount double precision,
    flvtimestart double precision,
    flvtimeend double precision,
    istepshe integer,
    istepsne integer,
    istepsar integer,
    nvcremarks character varying(200),
    blocked boolean DEFAULT false NOT NULL,
    extractionid integer,
    inlet_event smallint,
    inlet_note text
);


ALTER TABLE ngam.ngpreparations OWNER TO postgres;

--
-- Name: msrun_loadlist; Type: VIEW; Schema: ngam; Owner: postgres
--

CREATE VIEW ngam.msrun_loadlist AS
 SELECT 'NG'::character varying(2) AS measurement_mode,
    ngpreparations.inoblepreparationid AS loadlist_id,
    ngpreparations.runid,
    ngpreparations.positioninrun,
    ngpreparations.analysisid,
    ngpreparations.status,
    ngpreparations.bisblank,
    ngpreparations.bisreproreference,
    ngpreparations.bislinreference,
    ngpreparations.nvcremarks AS remarks,
    ngpreparations.inlet_event,
    ngpreparations.inlet_note,
    ngpreparations.blocked AS isrejected,
    ngpreparations.nvcinletstring,
    ngpreparations.iportnumber,
    ngpreparations.nvcreferencegas AS referencegas,
    ngpreparations.freferenceamount AS referenceamount,
    ngpreparations.flvtimestart,
    ngpreparations.flvtimeend,
    ngpreparations.istepshe,
    ngpreparations.istepsne,
    ngpreparations.istepsar,
    ngpreparations.extractionid,
    NULL::integer AS ingrowthid,
    NULL::integer AS precursorid,
    NULL::character varying(5) AS traynumber,
    NULL::smallint AS positionintray,
    NULL::smallint AS sampletype,
    NULL::real AS sampleamount,
    NULL::double precision AS measurementtime,
    NULL::double precision AS knownstdactivity,
    NULL::double precision AS knownstdactivityunc,
    NULL::smallint AS qc_decision,
    NULL::character varying(10) AS repeat_status,
    NULL::timestamp without time zone AS createdatestamp,
    NULL::character varying(50) AS createuserstamp
   FROM ngam.ngpreparations
UNION ALL
 SELECT 'IG'::character varying(2) AS measurement_mode,
    ng3hesequenceloadlist.headerid AS loadlist_id,
    ng3hesequenceloadlist.runid,
    ng3hesequenceloadlist.positioninrun,
    ng3hesequenceloadlist.analysisid,
    ng3hesequenceloadlist.status,
    ng3hesequenceloadlist.bisblank,
    ng3hesequenceloadlist.bisreproreference,
    ng3hesequenceloadlist.bislinreference,
    ng3hesequenceloadlist.remarks,
    ng3hesequenceloadlist.inlet_event,
    ng3hesequenceloadlist.inlet_note,
    ng3hesequenceloadlist.isrejected,
    NULL::character varying(100) AS nvcinletstring,
    NULL::integer AS iportnumber,
    NULL::character varying(50) AS referencegas,
    NULL::double precision AS referenceamount,
    NULL::double precision AS flvtimestart,
    NULL::double precision AS flvtimeend,
    NULL::integer AS istepshe,
    NULL::integer AS istepsne,
    NULL::integer AS istepsar,
    NULL::integer AS extractionid,
    ng3hesequenceloadlist.ingrowthid,
    ng3hesequenceloadlist.precursorid,
    ng3hesequenceloadlist.traynumber,
    ng3hesequenceloadlist.positionintray,
    ng3hesequenceloadlist.sampletype,
    ng3hesequenceloadlist.sampleamount,
    ng3hesequenceloadlist.measurementtime,
    ng3hesequenceloadlist.knownstdactivity,
    ng3hesequenceloadlist.knownstdactivityunc,
    ng3hesequenceloadlist.qc_decision,
    ng3hesequenceloadlist.repeat_status,
    ng3hesequenceloadlist.createdatestamp,
    ng3hesequenceloadlist.createuserstamp
   FROM ngam.ng3hesequenceloadlist;


ALTER VIEW ngam.msrun_loadlist OWNER TO postgres;

--
-- Name: msrun_runid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

ALTER TABLE ngam.msrun ALTER COLUMN runid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ngam.msrun_runid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: ng3heingrowthdata; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ng3heingrowthdata (
    ingrowthid integer NOT NULL,
    runid integer DEFAULT 0 NOT NULL,
    analysisid integer DEFAULT 0 NOT NULL,
    repeat integer,
    iposition integer DEFAULT 0 NOT NULL,
    status integer,
    fstaticleaktestbefore double precision,
    fdegassinghours double precision,
    dtimestart timestamp without time zone,
    dtimeend timestamp without time zone,
    fleaktestafter double precision,
    fweightwaterbulbempty double precision,
    fweightwaterbulbbefore double precision,
    fweightwaterbulbafter double precision,
    nvcremarks character varying(200),
    isignored smallint,
    itritiumsuccessorid integer
);


ALTER TABLE ngam.ng3heingrowthdata OWNER TO postgres;

--
-- Name: ng3heingrowthdata_ingrowthid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

ALTER TABLE ngam.ng3heingrowthdata ALTER COLUMN ingrowthid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ngam.ng3heingrowthdata_ingrowthid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: ng3heingrowthrun; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ng3heingrowthrun (
    runid integer NOT NULL,
    workflowid smallint,
    workflowjobid smallint,
    procedureid integer,
    equipmentid integer,
    runstarttime timestamp without time zone,
    runendtime timestamp without time zone,
    remarks character varying(255),
    technicianid integer,
    runstatus smallint,
    islocked boolean DEFAULT false NOT NULL,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE ngam.ng3heingrowthrun OWNER TO postgres;

--
-- Name: ng3heingrowthrun_runid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

ALTER TABLE ngam.ng3heingrowthrun ALTER COLUMN runid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ngam.ng3heingrowthrun_runid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: ng3hesequenceloadlist_headerid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

ALTER TABLE ngam.ng3hesequenceloadlist ALTER COLUMN headerid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ngam.ng3hesequenceloadlist_headerid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: ng3hesequenceraw; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ng3hesequenceraw (
    ng3hesequencerawid integer NOT NULL,
    headerid integer DEFAULT 0 NOT NULL,
    cycleno smallint DEFAULT 0 NOT NULL,
    valuekind smallint DEFAULT 0 NOT NULL,
    fvalue double precision,
    isrejected boolean DEFAULT false NOT NULL
);


ALTER TABLE ngam.ng3hesequenceraw OWNER TO postgres;

--
-- Name: ng3hesequenceraw_ng3hesequencerawid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

ALTER TABLE ngam.ng3hesequenceraw ALTER COLUMN ng3hesequencerawid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ngam.ng3hesequenceraw_ng3hesequencerawid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: ng3hesequenceresults; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ng3hesequenceresults (
    resultid integer NOT NULL,
    runid integer NOT NULL,
    headerid integer,
    positioninrun integer,
    analysisid integer,
    position_type character varying(20),
    n_cycles_used integer,
    mean4he_v double precision,
    se4he_v double precision,
    mean3he_a double precision,
    se3he_a double precision,
    bg_mean_a double precision,
    bg_se_a double precision,
    net3he_a double precision,
    net3he_unc_a double precision,
    sensitivity double precision,
    sensitivity_unc double precision,
    activity double precision,
    activity_unc double precision,
    activity_corrected double precision,
    activity_corrected_unc double precision,
    unitid integer DEFAULT 1 NOT NULL,
    ingrowth_correction_factor double precision,
    isblank boolean DEFAULT false,
    isstandard boolean DEFAULT false,
    isrejected boolean DEFAULT false,
    rejection_reason character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(100)
);


ALTER TABLE ngam.ng3hesequenceresults OWNER TO postgres;

--
-- Name: ng3hesequenceresults_resultid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

CREATE SEQUENCE ngam.ng3hesequenceresults_resultid_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE ngam.ng3hesequenceresults_resultid_seq OWNER TO postgres;

--
-- Name: ng3hesequenceresults_resultid_seq; Type: SEQUENCE OWNED BY; Schema: ngam; Owner: postgres
--

ALTER SEQUENCE ngam.ng3hesequenceresults_resultid_seq OWNED BY ngam.ng3hesequenceresults.resultid;


--
-- Name: ng3hesequencerun; Type: VIEW; Schema: ngam; Owner: postgres
--

CREATE VIEW ngam.ng3hesequencerun AS
 SELECT runid,
    workflowid,
    workflowjobid,
    equipmentid,
    procedureid,
    technicianid,
    runstarttime,
    runendtime,
    runstatus,
    islocked,
    remarks,
    createdatestamp,
    createuserstamp,
    modifdatestamp,
    modifuserstamp,
    datapath,
    headerstoimport,
    storedheaders,
    minutescompleted,
    meanbackground,
    meanbackgroundunc,
    meanstandard,
    meanstandardunc,
    outliermethod
   FROM ngam.msrun
  WHERE ((measurement_mode)::text = 'IG'::text);


ALTER VIEW ngam.ng3hesequencerun OWNER TO postgres;

--
-- Name: ng_cf_template; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ng_cf_template (
    template_id integer NOT NULL,
    serial_number integer NOT NULL,
    container_type_id smallint,
    equipmentid integer,
    calc_cf_he double precision,
    calc_err_he double precision,
    calc_cf_ne double precision,
    calc_err_ne double precision,
    calc_cf_ar double precision,
    calc_err_ar double precision,
    calc_cf_kr double precision,
    calc_err_kr double precision,
    calc_cf_xe double precision,
    calc_err_xe double precision,
    n_eqw_runs integer,
    applied_cf_he double precision,
    applied_err_he double precision,
    applied_cf_ne double precision,
    applied_err_ne double precision,
    applied_cf_ar double precision,
    applied_err_ar double precision,
    applied_cf_kr double precision,
    applied_err_kr double precision,
    applied_cf_xe double precision,
    applied_err_xe double precision,
    is_current boolean DEFAULT false NOT NULL,
    promoted_by character varying(50),
    promoted_at timestamp without time zone,
    notes text
);


ALTER TABLE ngam.ng_cf_template OWNER TO postgres;

--
-- Name: TABLE ng_cf_template; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON TABLE ngam.ng_cf_template IS 'Serial-numbered CF template per (container_type, equipment). is_current=TRUE marks the active template used for data reduction. calc_cf_* = mean of non-locked EQW runs.  applied_cf_* = long-running average promoted by admin analyst.';


--
-- Name: ng_cf_template_run; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ng_cf_template_run (
    template_id integer NOT NULL,
    eqw_run_id integer NOT NULL
);


ALTER TABLE ngam.ng_cf_template_run OWNER TO postgres;

--
-- Name: ng_cf_template_template_id_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

CREATE SEQUENCE ngam.ng_cf_template_template_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE ngam.ng_cf_template_template_id_seq OWNER TO postgres;

--
-- Name: ng_cf_template_template_id_seq; Type: SEQUENCE OWNED BY; Schema: ngam; Owner: postgres
--

ALTER SEQUENCE ngam.ng_cf_template_template_id_seq OWNED BY ngam.ng_cf_template.template_id;


--
-- Name: ng_eqw_run; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ng_eqw_run (
    eqw_run_id integer NOT NULL,
    analysisid integer,
    container_type_id smallint,
    equipmentid integer,
    temperature_c double precision,
    pressure_torr double precision,
    salinity_ppt double precision DEFAULT 0 NOT NULL,
    he_meas double precision,
    ne_meas double precision,
    ar_meas double precision,
    kr_meas double precision,
    xe_meas double precision,
    he_err double precision,
    ne_err double precision,
    ar_err double precision,
    kr_err double precision,
    xe_err double precision,
    he_eq double precision,
    ne_eq double precision,
    ar_eq double precision,
    kr_eq double precision,
    xe_eq double precision,
    cf_he double precision,
    cf_ne double precision,
    cf_ar double precision,
    cf_kr double precision,
    cf_xe double precision,
    lock_he smallint DEFAULT 0 NOT NULL,
    lock_ne smallint DEFAULT 0 NOT NULL,
    lock_ar smallint DEFAULT 0 NOT NULL,
    lock_kr smallint DEFAULT 0 NOT NULL,
    lock_xe smallint DEFAULT 0 NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    created_by character varying(50)
);


ALTER TABLE ngam.ng_eqw_run OWNER TO postgres;

--
-- Name: TABLE ng_eqw_run; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON TABLE ngam.ng_eqw_run IS 'One row per EQW sample after full MS data reduction. CF = eq/meas per gas.  lock_* = 1 excludes that gas from aggregate CF.';


--
-- Name: ng_eqw_run_eqw_run_id_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

CREATE SEQUENCE ngam.ng_eqw_run_eqw_run_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE ngam.ng_eqw_run_eqw_run_id_seq OWNER TO postgres;

--
-- Name: ng_eqw_run_eqw_run_id_seq; Type: SEQUENCE OWNED BY; Schema: ngam; Owner: postgres
--

ALTER SEQUENCE ngam.ng_eqw_run_eqw_run_id_seq OWNED BY ngam.ng_eqw_run.eqw_run_id;


--
-- Name: ng_pipette; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ng_pipette (
    id integer NOT NULL,
    pipette_name character varying(50) NOT NULL,
    control_type smallint DEFAULT 1 NOT NULL,
    valve_in integer DEFAULT 0 NOT NULL,
    valve_out integer DEFAULT 0 NOT NULL,
    select_switch integer DEFAULT 0 NOT NULL,
    volume double precision NOT NULL,
    vessel_name character varying(50) NOT NULL,
    initial_counter double precision DEFAULT 0 NOT NULL,
    actual_counter double precision DEFAULT 0 NOT NULL,
    is_active smallint DEFAULT 1 NOT NULL
);


ALTER TABLE ngam.ng_pipette OWNER TO postgres;

--
-- Name: ng_pipette_id_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

CREATE SEQUENCE ngam.ng_pipette_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE ngam.ng_pipette_id_seq OWNER TO postgres;

--
-- Name: ng_pipette_id_seq; Type: SEQUENCE OWNED BY; Schema: ngam; Owner: postgres
--

ALTER SEQUENCE ngam.ng_pipette_id_seq OWNED BY ngam.ng_pipette.id;


--
-- Name: ng_reference_vessel; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ng_reference_vessel (
    id integer NOT NULL,
    vessel_name character varying(50) NOT NULL,
    gas_name character varying(50) NOT NULL,
    volume double precision NOT NULL,
    tubing_volume double precision DEFAULT 0 NOT NULL,
    fill_pressure double precision,
    fill_temperature double precision,
    fill_humidity double precision,
    is_live_conditions smallint DEFAULT 0 NOT NULL,
    is_active smallint DEFAULT 1 NOT NULL
);


ALTER TABLE ngam.ng_reference_vessel OWNER TO postgres;

--
-- Name: ng_reference_vessel_id_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

CREATE SEQUENCE ngam.ng_reference_vessel_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE ngam.ng_reference_vessel_id_seq OWNER TO postgres;

--
-- Name: ng_reference_vessel_id_seq; Type: SEQUENCE OWNED BY; Schema: ngam; Owner: postgres
--

ALTER SEQUENCE ngam.ng_reference_vessel_id_seq OWNED BY ngam.ng_reference_vessel.id;


--
-- Name: ngbgproxyfactor; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngbgproxyfactor (
    id integer NOT NULL,
    equipmentid integer,
    factor_4he double precision NOT NULL,
    factor_4he_n integer DEFAULT 1,
    factor_4he_std double precision,
    source_runid integer,
    computed_at timestamp without time zone DEFAULT now() NOT NULL,
    created_by character varying(50) DEFAULT 'web_user'::character varying
);


ALTER TABLE ngam.ngbgproxyfactor OWNER TO postgres;

--
-- Name: ngbgproxyfactor_id_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

CREATE SEQUENCE ngam.ngbgproxyfactor_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE ngam.ngbgproxyfactor_id_seq OWNER TO postgres;

--
-- Name: ngbgproxyfactor_id_seq; Type: SEQUENCE OWNED BY; Schema: ngam; Owner: postgres
--

ALTER SEQUENCE ngam.ngbgproxyfactor_id_seq OWNED BY ngam.ngbgproxyfactor.id;


--
-- Name: ngblock; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngblock (
    iblockid integer NOT NULL,
    iheaderid integer DEFAULT 0 NOT NULL,
    nvcname character varying(50) DEFAULT ''::character varying NOT NULL,
    bonfaraday boolean DEFAULT false NOT NULL,
    nvcbackgroundtobeused character varying(50),
    blocked boolean DEFAULT false NOT NULL
);


ALTER TABLE ngam.ngblock OWNER TO postgres;

--
-- Name: ngblock_iblockid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

ALTER TABLE ngam.ngblock ALTER COLUMN iblockid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ngam.ngblock_iblockid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: ngblockevaluation; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngblockevaluation (
    iblockid integer NOT NULL,
    fblank double precision,
    fblankuncertainty double precision,
    fefficiency double precision,
    fefficiencyuncertainty double precision,
    flinearity double precision,
    flinearityuncertainty double precision,
    fqafield double precision,
    fqafielduncertainty double precision,
    fccstp double precision,
    fccstpuncertainty double precision,
    fccstppergram double precision,
    fccstppergramuncertainty double precision,
    fvalue double precision,
    funcertainty double precision,
    inoblevalueid integer
);


ALTER TABLE ngam.ngblockevaluation OWNER TO postgres;

--
-- Name: ngblockfit; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngblockfit (
    iblockid integer NOT NULL,
    fsignalatreferencetime double precision,
    fsignalatreferencetimeuncertainty double precision,
    fintercept double precision,
    finterceptuncertainty double precision,
    fslope double precision,
    fslopeuncertainty double precision,
    fcorrelation double precision,
    fnetintercept double precision,
    fnetinterceptuncertainty double precision,
    fnetslope double precision,
    fnetslopeuncertainty double precision,
    fnetcorrelation double precision,
    sikind smallint DEFAULT 0 NOT NULL,
    sikindnet smallint DEFAULT 0 NOT NULL
);


ALTER TABLE ngam.ngblockfit OWNER TO postgres;

--
-- Name: ngdilutionfactor; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngdilutionfactor (
    dilutionid integer NOT NULL,
    equipmentid integer,
    runid integer,
    valid_from timestamp with time zone NOT NULL,
    valid_until timestamp with time zone,
    element character(2) NOT NULL,
    dilution_factor double precision NOT NULL,
    dilution_factor_unc double precision,
    method character varying(60),
    notes text,
    createdatestamp timestamp with time zone DEFAULT now() NOT NULL,
    createuserstamp character varying(60),
    CONSTRAINT ngdilutionfactor_dilution_factor_check CHECK ((dilution_factor >= (1.0)::double precision)),
    CONSTRAINT ngdilutionfactor_dilution_factor_unc_check CHECK (((dilution_factor_unc IS NULL) OR (dilution_factor_unc >= (0.0)::double precision))),
    CONSTRAINT ngdilutionfactor_element_check CHECK ((element = ANY (ARRAY['He'::bpchar, 'Ne'::bpchar])))
);


ALTER TABLE ngam.ngdilutionfactor OWNER TO postgres;

--
-- Name: ngdilutionfactor_dilutionid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

CREATE SEQUENCE ngam.ngdilutionfactor_dilutionid_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE ngam.ngdilutionfactor_dilutionid_seq OWNER TO postgres;

--
-- Name: ngdilutionfactor_dilutionid_seq; Type: SEQUENCE OWNED BY; Schema: ngam; Owner: postgres
--

ALTER SEQUENCE ngam.ngdilutionfactor_dilutionid_seq OWNED BY ngam.ngdilutionfactor.dilutionid;


--
-- Name: ngextractiondata; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngextractiondata (
    runid integer NOT NULL,
    iposition integer DEFAULT 0 NOT NULL,
    analysisid integer DEFAULT 0 NOT NULL,
    fstaticleaktestbefore double precision,
    dtimestart timestamp without time zone,
    dtimeend timestamp without time zone,
    fleaktestafter double precision,
    fweighttubebefore double precision,
    fweighttubeafter double precision,
    fweightgasbulbbefore double precision,
    fweightgasbulbafter double precision,
    fweightwaterbulbbefore double precision,
    fweightwaterbulbafter double precision,
    status integer,
    nvcremarks character varying(200),
    isignored smallint,
    igassuccessorid integer,
    temperature_c double precision,
    salinity_ppt double precision,
    altitude_m double precision,
    extraction_efficiency double precision,
    container_type smallint DEFAULT 1 NOT NULL,
    sample_volume_ml double precision,
    lab_pressure_torr double precision,
    extractionid integer NOT NULL,
    CONSTRAINT chk_container_type CHECK ((container_type = ANY (ARRAY[1, 2, 3]))),
    CONSTRAINT chk_extraction_altitude_range CHECK (((altitude_m IS NULL) OR ((altitude_m >= ('-500.0'::numeric)::double precision) AND (altitude_m <= (9000.0)::double precision)))),
    CONSTRAINT chk_extraction_efficiency_range CHECK (((extraction_efficiency IS NULL) OR ((extraction_efficiency > (0.0)::double precision) AND (extraction_efficiency <= (1.0)::double precision)))),
    CONSTRAINT chk_extraction_salinity_range CHECK (((salinity_ppt IS NULL) OR ((salinity_ppt >= (0.0)::double precision) AND (salinity_ppt <= (50.0)::double precision)))),
    CONSTRAINT chk_extraction_temperature_range CHECK (((temperature_c IS NULL) OR ((temperature_c >= ('-5.0'::numeric)::double precision) AND (temperature_c <= (50.0)::double precision)))),
    CONSTRAINT chk_lab_pressure_torr_range CHECK (((lab_pressure_torr IS NULL) OR ((lab_pressure_torr >= (600.0)::double precision) AND (lab_pressure_torr <= (800.0)::double precision))))
);


ALTER TABLE ngam.ngextractiondata OWNER TO postgres;

--
-- Name: COLUMN ngextractiondata.temperature_c; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractiondata.temperature_c IS 'Water temperature at time of field sampling (°C). Used in Weiss (1971) solubility equations to compute C_eq for each noble gas isotope.';


--
-- Name: COLUMN ngextractiondata.salinity_ppt; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractiondata.salinity_ppt IS 'Water salinity (g/kg = ‰). 0 for fresh water; ~35 for open ocean. Reduces noble gas solubility via the Setchenow term in the Weiss equations.';


--
-- Name: COLUMN ngextractiondata.altitude_m; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractiondata.altitude_m IS 'Altitude of sampling site above sea level (m). Used to derive local atmospheric pressure: P = exp(−h/8500) atm. NULL treated as sea level (P = 1.0 atm).';


--
-- Name: COLUMN ngextractiondata.extraction_efficiency; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractiondata.extraction_efficiency IS 'Measured extraction efficiency (dimensionless, 0 < η ≤ 1.0). Corrected dissolved concentration = measured_ccSTP / η. NULL or 1.0 = no correction applied.';


--
-- Name: COLUMN ngextractiondata.container_type; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractiondata.container_type IS 'Sample container / extraction method: 1 = water-bulb (water collected gravimetrically into glass bulb), 2 = Cu-tube (sample sealed in copper tube; tube loses mass on extraction), 3 = diffusion sampler (passive equilibration; water mass from sample_volume_ml).';


--
-- Name: COLUMN ngextractiondata.sample_volume_ml; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractiondata.sample_volume_ml IS 'Water volume (mL) for diffusion-sampler positions (container_type = 3 on public.sample). Approximated as grams for concentration normalisation (ρ_water ≈ 1 g/mL). NULL for water-bulb and Cu-tube positions.';


--
-- Name: COLUMN ngextractiondata.lab_pressure_torr; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractiondata.lab_pressure_torr IS 'Measured barometric pressure in Torr at time of EQW equilibration. When non-NULL, overrides altitude_m for equilibrium solubility computation (pressure_atm = lab_pressure_torr / 760.0).  Null for normal field samples.';


--
-- Name: ngextractiondata_extractionid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

CREATE SEQUENCE ngam.ngextractiondata_extractionid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE ngam.ngextractiondata_extractionid_seq OWNER TO postgres;

--
-- Name: ngextractiondata_extractionid_seq; Type: SEQUENCE OWNED BY; Schema: ngam; Owner: postgres
--

ALTER SEQUENCE ngam.ngextractiondata_extractionid_seq OWNED BY ngam.ngextractiondata.extractionid;


--
-- Name: ngextractionlineefficiency; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngextractionlineefficiency (
    efficiencyid integer NOT NULL,
    equipmentid integer,
    runid integer,
    valid_from timestamp with time zone NOT NULL,
    valid_until timestamp with time zone,
    element character(2) NOT NULL,
    efficiency double precision NOT NULL,
    efficiency_unc double precision,
    method character varying(60),
    notes text,
    createdatestamp timestamp with time zone DEFAULT now() NOT NULL,
    createuserstamp character varying(60),
    CONSTRAINT ngextractionlineefficiency_efficiency_check CHECK (((efficiency > (0.0)::double precision) AND (efficiency <= (1.0)::double precision))),
    CONSTRAINT ngextractionlineefficiency_efficiency_unc_check CHECK (((efficiency_unc IS NULL) OR (efficiency_unc >= (0.0)::double precision))),
    CONSTRAINT ngextractionlineefficiency_element_check CHECK ((element = ANY (ARRAY['He'::bpchar, 'Ne'::bpchar, 'Ar'::bpchar, 'Kr'::bpchar, 'Xe'::bpchar])))
);


ALTER TABLE ngam.ngextractionlineefficiency OWNER TO postgres;

--
-- Name: TABLE ngextractionlineefficiency; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON TABLE ngam.ngextractionlineefficiency IS 'Per-element extraction efficiency calibrations for each noble gas vacuum line / instrument. One row per element per calibration event; temporal validity controlled by valid_from / valid_until.';


--
-- Name: COLUMN ngextractionlineefficiency.equipmentid; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractionlineefficiency.equipmentid IS 'Instrument the calibration applies to (FK → public.equipment). NULL = lab-wide.';


--
-- Name: COLUMN ngextractionlineefficiency.runid; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractionlineefficiency.runid IS 'Optional link to the extraction run in which this η was measured (informational).';


--
-- Name: COLUMN ngextractionlineefficiency.valid_from; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractionlineefficiency.valid_from IS 'Start of validity window (inclusive). Use the date the calibration was performed.';


--
-- Name: COLUMN ngextractionlineefficiency.valid_until; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractionlineefficiency.valid_until IS 'End of validity window (exclusive). NULL = still current. Set to now() when retiring.';


--
-- Name: COLUMN ngextractionlineefficiency.element; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractionlineefficiency.element IS 'Noble gas element: He, Ne, Ar, Kr, or Xe.';


--
-- Name: COLUMN ngextractionlineefficiency.efficiency; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractionlineefficiency.efficiency IS 'Fractional extraction efficiency η (0 < η ≤ 1.0). ccSTP_true = ccSTP_measured / η.';


--
-- Name: COLUMN ngextractionlineefficiency.efficiency_unc; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractionlineefficiency.efficiency_unc IS '1-sigma uncertainty on η. Propagated into ccSTP_true_unc.';


--
-- Name: COLUMN ngextractionlineefficiency.method; Type: COMMENT; Schema: ngam; Owner: postgres
--

COMMENT ON COLUMN ngam.ngextractionlineefficiency.method IS 'Method used to determine η: double_extraction, theoretical, standard, or other.';


--
-- Name: ngextractionlineefficiency_efficiencyid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

CREATE SEQUENCE ngam.ngextractionlineefficiency_efficiencyid_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE ngam.ngextractionlineefficiency_efficiencyid_seq OWNER TO postgres;

--
-- Name: ngextractionlineefficiency_efficiencyid_seq; Type: SEQUENCE OWNED BY; Schema: ngam; Owner: postgres
--

ALTER SEQUENCE ngam.ngextractionlineefficiency_efficiencyid_seq OWNED BY ngam.ngextractionlineefficiency.efficiencyid;


--
-- Name: ngextractionrun; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngextractionrun (
    runid integer NOT NULL,
    workflowid smallint,
    workflowjobid smallint,
    procedureid integer,
    equipmentid integer,
    runstarttime timestamp without time zone,
    runendtime timestamp without time zone,
    remarks character varying(255),
    technicianid integer,
    runstatus smallint,
    islocked boolean DEFAULT false NOT NULL,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE ngam.ngextractionrun OWNER TO postgres;

--
-- Name: ngextractionrun_runid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

ALTER TABLE ngam.ngextractionrun ALTER COLUMN runid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ngam.ngextractionrun_runid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: nggaugeresult; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.nggaugeresult (
    runid integer NOT NULL,
    positioninrun integer NOT NULL,
    element text NOT NULL,
    conc double precision,
    conc_unc double precision,
    conc_per_g double precision,
    conc_per_g_unc double precision
);


ALTER TABLE ngam.nggaugeresult OWNER TO postgres;

--
-- Name: ngheaders; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngheaders (
    inobleheaderid integer NOT NULL,
    runid integer,
    ipreparationid integer,
    analysisid integer,
    nvcmeasurementfile character varying(255),
    equipmentid integer,
    nvcnuclidestoevaluate character varying(255),
    nvcinletstring character varying(100),
    flvtimeinletstart double precision,
    flvtimeinletstop double precision,
    flvtimepumpdownstart double precision,
    flvtimepumpdownstop double precision,
    freferencetime double precision NOT NULL,
    nvcremarks character varying(200),
    blocked boolean DEFAULT false NOT NULL
);


ALTER TABLE ngam.ngheaders OWNER TO postgres;

--
-- Name: ngheaders_inobleheaderid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

ALTER TABLE ngam.ngheaders ALTER COLUMN inobleheaderid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ngam.ngheaders_inobleheaderid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: nglinearitysnapshots; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.nglinearitysnapshots (
    snapshot_id integer NOT NULL,
    ourlabid character varying(50) NOT NULL,
    isotope_key character varying(30) NOT NULL,
    signal_level double precision NOT NULL,
    sensitivity double precision NOT NULL,
    run_id integer NOT NULL,
    createdatestamp timestamp without time zone DEFAULT now() NOT NULL
);


ALTER TABLE ngam.nglinearitysnapshots OWNER TO postgres;

--
-- Name: nglinearitysnapshots_snapshot_id_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

CREATE SEQUENCE ngam.nglinearitysnapshots_snapshot_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE ngam.nglinearitysnapshots_snapshot_id_seq OWNER TO postgres;

--
-- Name: nglinearitysnapshots_snapshot_id_seq; Type: SEQUENCE OWNED BY; Schema: ngam; Owner: postgres
--

ALTER SEQUENCE ngam.nglinearitysnapshots_snapshot_id_seq OWNED BY ngam.nglinearitysnapshots.snapshot_id;


--
-- Name: ngpreparationevent; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngpreparationevent (
    ingpreparationid integer NOT NULL,
    ingqualifierid integer DEFAULT 0 NOT NULL,
    flvtime double precision NOT NULL,
    fngprepvalue double precision,
    nvcngprepstring character varying(200)
);


ALTER TABLE ngam.ngpreparationevent OWNER TO postgres;

--
-- Name: ngpreparations_inoblepreparationid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

ALTER TABLE ngam.ngpreparations ALTER COLUMN inoblepreparationid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ngam.ngpreparations_inoblepreparationid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: ngpreparationstep; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngpreparationstep (
    ipreparationid integer NOT NULL,
    instepqualifier integer DEFAULT 0 NOT NULL,
    flvtime double precision NOT NULL,
    fsignal double precision,
    funcertainty double precision,
    fqafield double precision,
    fqafielduncertainty double precision,
    fblank double precision,
    fblankuncertainty double precision,
    fefficiency double precision,
    fefficiencyuncertainty double precision,
    flinearity double precision,
    flinearityuncertainty double precision,
    fccstp double precision,
    fccstpuncertainty double precision,
    fccstppergram double precision,
    fccstppergramuncertainty double precision,
    nvcevaluationdescriptor character varying(200),
    blocked boolean DEFAULT false NOT NULL,
    inoblevalueid integer
);


ALTER TABLE ngam.ngpreparationstep OWNER TO postgres;

--
-- Name: ngpreparationstepqualifier; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngpreparationstepqualifier (
    inoblestepqualifierid integer NOT NULL,
    nvcstepqualifiername character varying(100) DEFAULT ''::character varying NOT NULL,
    instartqualifier integer DEFAULT 0 NOT NULL,
    instopqualifier integer DEFAULT 0 NOT NULL,
    nvcsignalname character varying(50),
    nvcswitchname character varying(50),
    fnormalstartinterval double precision NOT NULL,
    fnormalendinterval double precision NOT NULL,
    fnormaltimebeforestart double precision NOT NULL,
    fnormaltimeafterend double precision NOT NULL,
    inormalstartfit smallint DEFAULT 0 NOT NULL,
    inormalendfit smallint DEFAULT 0 NOT NULL,
    inresultqualifier integer
);


ALTER TABLE ngam.ngpreparationstepqualifier OWNER TO postgres;

--
-- Name: ngpreparationstepqualifier_inoblestepqualifierid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

ALTER TABLE ngam.ngpreparationstepqualifier ALTER COLUMN inoblestepqualifierid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ngam.ngpreparationstepqualifier_inoblestepqualifierid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: ngpreparationstepsignal; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngpreparationstepsignal (
    ipreparationid integer NOT NULL,
    instepqualifier integer DEFAULT 0 NOT NULL,
    flvtime double precision NOT NULL,
    fsignal double precision NOT NULL,
    bswitch boolean
);


ALTER TABLE ngam.ngpreparationstepsignal OWNER TO postgres;

--
-- Name: ngqualifier; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngqualifier (
    inoblequalifierid integer NOT NULL,
    nvcqualifiername character varying(100) DEFAULT ''::character varying NOT NULL,
    nvcbeforeinseqid character varying(100) DEFAULT ''::character varying NOT NULL,
    nvcafterinseqid character varying(100),
    bactive boolean DEFAULT false NOT NULL,
    bhasvalue boolean DEFAULT false NOT NULL,
    bhasstring boolean DEFAULT false NOT NULL
);


ALTER TABLE ngam.ngqualifier OWNER TO postgres;

--
-- Name: ngqualifier_inoblequalifierid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

ALTER TABLE ngam.ngqualifier ALTER COLUMN inoblequalifierid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ngam.ngqualifier_inoblequalifierid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: ngratioresult; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngratioresult (
    runid integer NOT NULL,
    positioninrun integer NOT NULL,
    ratio_name text NOT NULL,
    raw_ratio double precision,
    raw_ratio_unc double precision,
    blank_corrected double precision,
    blank_corrected_unc double precision,
    drift_corrected double precision,
    drift_corrected_unc double precision
);


ALTER TABLE ngam.ngratioresult OWNER TO postgres;

--
-- Name: ngreference; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngreference (
    ngreferenceid integer NOT NULL,
    referencedataid integer,
    nvcgasname character varying(50) NOT NULL,
    nvcspecies character varying(50) DEFAULT ''::character varying NOT NULL,
    nvcelement character varying(10) DEFAULT ''::character varying NOT NULL,
    device character varying(10) DEFAULT 'MS'::character varying NOT NULL,
    bisratio boolean DEFAULT false NOT NULL,
    nvcremarks character varying(200)
);


ALTER TABLE ngam.ngreference OWNER TO postgres;

--
-- Name: ngreference_ngreferenceid_seq; Type: SEQUENCE; Schema: ngam; Owner: postgres
--

ALTER TABLE ngam.ngreference ALTER COLUMN ngreferenceid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ngam.ngreference_ngreferenceid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: ngsequenceevaluation; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngsequenceevaluation (
    runid integer CONSTRAINT ngsequenceevaluation_isequenceid_not_null NOT NULL,
    nvcspecies character varying(50) DEFAULT ''::character varying NOT NULL,
    bfromprep boolean DEFAULT false NOT NULL,
    flvfitbstart double precision NOT NULL,
    flvfitbend double precision NOT NULL,
    flvfitestart double precision,
    flvfiteend double precision,
    flvfitlstart double precision,
    flvfitlend double precision,
    iblankfitdegree integer DEFAULT 0 NOT NULL,
    bblankfitweight boolean DEFAULT false NOT NULL,
    iefficiencyfitdegree integer DEFAULT 0 NOT NULL,
    befficiencyfitweight boolean DEFAULT false NOT NULL,
    ilinearityfitdegree integer DEFAULT 0 NOT NULL,
    blinearityfitweight boolean DEFAULT false NOT NULL,
    nvclinearityreferencesignals character varying(100),
    iprocedureid integer,
    blocked boolean DEFAULT false NOT NULL
);


ALTER TABLE ngam.ngsequenceevaluation OWNER TO postgres;

--
-- Name: ngsequencefit; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngsequencefit (
    runid integer CONSTRAINT ngsequencefit_isequenceid_not_null NOT NULL,
    nvcspecies character varying(50) DEFAULT ''::character varying NOT NULL,
    ccoefficientkind character varying(1) DEFAULT ''::character varying NOT NULL,
    icoefficientnumber integer DEFAULT 0 NOT NULL,
    fcoefficientvalue double precision,
    fcoefficientuncertainty double precision
);


ALTER TABLE ngam.ngsequencefit OWNER TO postgres;

--
-- Name: ngsequencerun; Type: VIEW; Schema: ngam; Owner: postgres
--

CREATE VIEW ngam.ngsequencerun AS
 SELECT runid,
    workflowid,
    equipmentid,
    procedureid,
    technicianid,
    runstarttime,
    runendtime,
    runstatus,
    islocked,
    remarks,
    createdatestamp,
    createuserstamp,
    modifdatestamp,
    modifuserstamp,
    nvcprotocolfilepath,
    nvcstatusfilepath,
    bg_proxy_mode,
    bg_proxy_factor_4he
   FROM ngam.msrun
  WHERE ((measurement_mode)::text = 'NG'::text);


ALTER VIEW ngam.ngsequencerun OWNER TO postgres;

--
-- Name: ngsignal; Type: TABLE; Schema: ngam; Owner: postgres
--

CREATE TABLE ngam.ngsignal (
    iblockid integer NOT NULL,
    flvtime double precision NOT NULL,
    fsignal double precision NOT NULL,
    blocked boolean DEFAULT false NOT NULL
);


ALTER TABLE ngam.ngsignal OWNER TO postgres;

--
-- Name: public_analysis_analysisid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_analysis_analysisid_seq
    START WITH 101116
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_analysis_analysisid_seq OWNER TO postgres;

--
-- Name: analysis; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.analysis (
    analysisid integer DEFAULT nextval('public.public_analysis_analysisid_seq'::regclass) NOT NULL,
    sampleid integer DEFAULT 0 NOT NULL,
    prefix character varying(1),
    workflowid smallint DEFAULT 0 NOT NULL,
    repeats smallint DEFAULT 1 NOT NULL,
    status smallint DEFAULT 0 NOT NULL,
    precursoranalysisid integer,
    remarks character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50),
    phase smallint DEFAULT 0 NOT NULL
);


ALTER TABLE public.analysis OWNER TO postgres;

--
-- Name: public_analysis_repeat_analysisid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_analysis_repeat_analysisid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_analysis_repeat_analysisid_seq OWNER TO postgres;

--
-- Name: analysis_repeat; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.analysis_repeat (
    analysisid integer DEFAULT nextval('public.public_analysis_repeat_analysisid_seq'::regclass) NOT NULL,
    repeat smallint DEFAULT 0 NOT NULL,
    status smallint
);


ALTER TABLE public.analysis_repeat OWNER TO postgres;

--
-- Name: public_analysisprocedure_procedureid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_analysisprocedure_procedureid_seq
    START WITH 14003
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_analysisprocedure_procedureid_seq OWNER TO postgres;

--
-- Name: analysisprocedure; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.analysisprocedure (
    procedureid integer DEFAULT nextval('public.public_analysisprocedure_procedureid_seq'::regclass) NOT NULL,
    procedurename character varying(50) DEFAULT ''::character varying NOT NULL,
    categoryid integer,
    defaultdeviceid integer,
    loadliststring character varying(355),
    measurableid integer,
    eductmaterial character varying(50),
    productmaterial character varying(50),
    measurableslist character varying(255),
    numberofsamples integer,
    samplesize double precision,
    sampleexportformat smallint,
    analysisimportformat smallint,
    remarks character varying(255),
    reportingtextmemo character varying(500),
    isvirtual boolean,
    isobsolete boolean,
    importedprocedureid integer,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.analysisprocedure OWNER TO postgres;

--
-- Name: public_analysisprocedure_measurable_procedureid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_analysisprocedure_measurable_procedureid_seq
    START WITH 14003
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_analysisprocedure_measurable_procedureid_seq OWNER TO postgres;

--
-- Name: analysisprocedure_measurable; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.analysisprocedure_measurable (
    procedureid integer DEFAULT nextval('public.public_analysisprocedure_measurable_procedureid_seq'::regclass) NOT NULL,
    measurableid smallint DEFAULT 0 NOT NULL,
    repeats smallint,
    detectionlimit double precision,
    accuracylimit double precision,
    repeatacceptancepercent smallint
);


ALTER TABLE public.analysisprocedure_measurable OWNER TO postgres;

--
-- Name: public_analysisprocedure_postprocessing_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_analysisprocedure_postprocessing_id_seq
    START WITH 34
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_analysisprocedure_postprocessing_id_seq OWNER TO postgres;

--
-- Name: analysisprocedure_postprocessing; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.analysisprocedure_postprocessing (
    id integer DEFAULT nextval('public.public_analysisprocedure_postprocessing_id_seq'::regclass) NOT NULL,
    procedureid integer DEFAULT 0 NOT NULL,
    correctiontype smallint DEFAULT 0 NOT NULL,
    correctionname character varying(255),
    correctionsubtype smallint,
    runsequence smallint,
    ourlabid character varying(20),
    remarks character varying(255)
);


ALTER TABLE public.analysisprocedure_postprocessing OWNER TO postgres;

--
-- Name: public_analysisprocedure_template_procedureid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_analysisprocedure_template_procedureid_seq
    START WITH 5015
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_analysisprocedure_template_procedureid_seq OWNER TO postgres;

--
-- Name: analysisprocedure_template; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.analysisprocedure_template (
    procedureid integer DEFAULT nextval('public.public_analysisprocedure_template_procedureid_seq'::regclass) NOT NULL,
    ordinalposition integer DEFAULT 0 NOT NULL,
    templatename character varying(50) DEFAULT ''::character varying NOT NULL,
    pageno integer DEFAULT 0 NOT NULL,
    pageposition integer DEFAULT 0 NOT NULL,
    portno character varying(6) DEFAULT ''::character varying NOT NULL,
    trayno smallint,
    vialno smallint,
    portrefempty integer DEFAULT 0 NOT NULL,
    injections smallint,
    prepinjections smallint,
    ignoredinjections smallint,
    aliquotsize integer,
    refourlabid character varying(9),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50),
    sampletype smallint,
    templateid integer
);


ALTER TABLE public.analysisprocedure_template OWNER TO postgres;

--
-- Name: analytes; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.analytes (
    analyteid integer NOT NULL,
    analytename text NOT NULL,
    parameterlabel character varying(100),
    descriptionid integer,
    unitid integer,
    matrixid integer,
    isfieldparam smallint DEFAULT 0 NOT NULL,
    halflife double precision,
    moduleid integer
);


ALTER TABLE public.analytes OWNER TO postgres;

--
-- Name: app_log; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.app_log (
    log_id bigint NOT NULL,
    logged_at timestamp with time zone DEFAULT now() NOT NULL,
    level text NOT NULL,
    module text,
    message text NOT NULL,
    extra jsonb
);


ALTER TABLE public.app_log OWNER TO postgres;

--
-- Name: app_log_log_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.app_log_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.app_log_log_id_seq OWNER TO postgres;

--
-- Name: app_log_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.app_log_log_id_seq OWNED BY public.app_log.log_id;


--
-- Name: chemenrprocedure; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.chemenrprocedure (
    procedureid integer NOT NULL,
    measurableid integer,
    initial_volume_ml double precision,
    final_volume_ml double precision,
    carrier_mass_g double precision,
    spike_added_dpm double precision,
    precipitate_type character varying(20),
    tare_weight_g double precision,
    gross_weight_g double precision,
    initial_stable_conc_mg_l double precision,
    final_stable_conc_mg_l double precision
);


ALTER TABLE public.chemenrprocedure OWNER TO postgres;

--
-- Name: chemenrsystem; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.chemenrsystem (
    chemenrsystemid integer NOT NULL,
    max_vials integer DEFAULT 12 NOT NULL,
    is_automatic boolean DEFAULT false NOT NULL
);


ALTER TABLE public.chemenrsystem OWNER TO postgres;

--
-- Name: cims_item; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.cims_item (
    item_id integer NOT NULL,
    item_name character varying(255) NOT NULL,
    category character varying(50) NOT NULL,
    cas_number character varying(20),
    unit_of_measure character varying(20) NOT NULL,
    reorder_point numeric(12,3) DEFAULT 0 NOT NULL,
    reorder_qty numeric(12,3),
    storage_location character varying(100),
    is_active smallint DEFAULT 1 NOT NULL,
    remarks text,
    createdatestamp timestamp without time zone DEFAULT now(),
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.cims_item OWNER TO postgres;

--
-- Name: cims_item_item_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.cims_item_item_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.cims_item_item_id_seq OWNER TO postgres;

--
-- Name: cims_item_item_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.cims_item_item_id_seq OWNED BY public.cims_item.item_id;


--
-- Name: cims_lot; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.cims_lot (
    lot_id integer NOT NULL,
    item_id integer NOT NULL,
    supplier_id integer,
    lot_number character varying(100),
    date_received date DEFAULT CURRENT_DATE NOT NULL,
    expiry_date date,
    qty_received numeric(12,3) NOT NULL,
    qty_remaining numeric(12,3) NOT NULL,
    unit_cost numeric(12,4),
    invoice_ref character varying(100),
    is_obsolete smallint DEFAULT 0 NOT NULL,
    remarks text,
    createdatestamp timestamp without time zone DEFAULT now(),
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.cims_lot OWNER TO postgres;

--
-- Name: cims_lot_lot_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.cims_lot_lot_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.cims_lot_lot_id_seq OWNER TO postgres;

--
-- Name: cims_lot_lot_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.cims_lot_lot_id_seq OWNED BY public.cims_lot.lot_id;


--
-- Name: cims_supplier; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.cims_supplier (
    supplier_id integer NOT NULL,
    supplier_name character varying(255) NOT NULL,
    category character varying(50),
    contact_name character varying(100),
    phone character varying(30),
    email character varying(150),
    website character varying(200),
    is_active smallint DEFAULT 1 NOT NULL,
    remarks text,
    createdatestamp timestamp without time zone DEFAULT now(),
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.cims_supplier OWNER TO postgres;

--
-- Name: cims_supplier_supplier_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.cims_supplier_supplier_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.cims_supplier_supplier_id_seq OWNER TO postgres;

--
-- Name: cims_supplier_supplier_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.cims_supplier_supplier_id_seq OWNED BY public.cims_supplier.supplier_id;


--
-- Name: cims_usage; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.cims_usage (
    usage_id bigint NOT NULL,
    lot_id integer NOT NULL,
    movement_type character varying(20) NOT NULL,
    quantity numeric(12,3) NOT NULL,
    movement_date timestamp without time zone DEFAULT now() NOT NULL,
    used_by character varying(50),
    analysisid integer,
    run_module character varying(10),
    run_id integer,
    purpose character varying(255),
    createdatestamp timestamp without time zone DEFAULT now(),
    createuserstamp character varying(50)
);


ALTER TABLE public.cims_usage OWNER TO postgres;

--
-- Name: cims_usage_usage_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.cims_usage_usage_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.cims_usage_usage_id_seq OWNER TO postgres;

--
-- Name: cims_usage_usage_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.cims_usage_usage_id_seq OWNED BY public.cims_usage.usage_id;


--
-- Name: container_type_lookup; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.container_type_lookup (
    container_type_id smallint NOT NULL,
    type_name character varying(60) NOT NULL,
    description text,
    is_active boolean DEFAULT true NOT NULL
);


ALTER TABLE public.container_type_lookup OWNER TO postgres;

--
-- Name: TABLE container_type_lookup; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON TABLE public.container_type_lookup IS 'Controlled vocabulary for sample container types. Referenced by public.sample.container_type and used by the NGAM extraction pipeline to select the correct water-mass calculation.';


--
-- Name: public_counterinstrument_equipmentid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_counterinstrument_equipmentid_seq
    START WITH 3005
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_counterinstrument_equipmentid_seq OWNER TO postgres;

--
-- Name: counterinstrument; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.counterinstrument (
    equipmentid integer DEFAULT nextval('public.public_counterinstrument_equipmentid_seq'::regclass) NOT NULL,
    processingtype integer,
    cpmwindowno smallint,
    sqpwindowno smallint,
    isobsolete boolean,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.counterinstrument OWNER TO postgres;

--
-- Name: country; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.country (
    sname character varying(100),
    countrycode character varying(2) DEFAULT ''::character varying NOT NULL,
    iso3166countrycode character varying(3),
    createdatestamp timestamp without time zone
);


ALTER TABLE public.country OWNER TO postgres;

--
-- Name: public_customer_customerid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_customer_customerid_seq
    START WITH 236
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_customer_customerid_seq OWNER TO postgres;

--
-- Name: customer; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.customer (
    customerid integer DEFAULT nextval('public.public_customer_customerid_seq'::regclass) NOT NULL,
    countrycode character varying(2),
    lastname character varying(50),
    firstname character varying(16),
    middlename character varying(9),
    institutionname character varying(50),
    mailstop character varying(35),
    streetname character varying(50),
    cityname character varying(22),
    postalcode character varying(10),
    phonenumber character varying(24),
    faxnumber character varying(18),
    email character varying(35),
    isobsolete boolean DEFAULT false NOT NULL,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.customer OWNER TO postgres;

--
-- Name: descriptions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.descriptions (
    descriptionid integer NOT NULL,
    header character varying(100) DEFAULT ''::character varying NOT NULL,
    fulldescription text DEFAULT ''::text NOT NULL
);


ALTER TABLE public.descriptions OWNER TO postgres;

--
-- Name: descriptions_descriptionid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.descriptions ALTER COLUMN descriptionid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.descriptions_descriptionid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: public_dilutionbatch_batchid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_dilutionbatch_batchid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_dilutionbatch_batchid_seq OWNER TO postgres;

--
-- Name: dilutionbatch; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.dilutionbatch (
    batchid integer DEFAULT nextval('public.public_dilutionbatch_batchid_seq'::regclass) NOT NULL,
    referenceid integer,
    coldsampleid integer,
    hotsampleid integer,
    coldsampleweight double precision,
    coldsampleweightunc double precision,
    hotsampleweight double precision,
    hotsampleweightunc double precision,
    dilutiondate timestamp without time zone,
    remarks character varying(50),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.dilutionbatch OWNER TO postgres;

--
-- Name: dilutiondata; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.dilutiondata (
    dilutiondataid integer NOT NULL,
    intnumber smallint,
    batchid integer,
    sampleid integer,
    prefix character varying(1),
    weightbefore double precision,
    weightbeforeunc double precision,
    weightafter double precision,
    weightafterunc double precision,
    netweight double precision,
    netweightunc double precision,
    remarks character varying(50)
);


ALTER TABLE public.dilutiondata OWNER TO postgres;

--
-- Name: dilutiondata_dilutiondataid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.dilutiondata ALTER COLUMN dilutiondataid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.dilutiondata_dilutiondataid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: public_distillationprocedure_procedureid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_distillationprocedure_procedureid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_distillationprocedure_procedureid_seq OWNER TO postgres;

--
-- Name: distillationprocedure; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.distillationprocedure (
    procedureid integer DEFAULT nextval('public.public_distillationprocedure_procedureid_seq'::regclass) NOT NULL,
    firstbulbid smallint
);


ALTER TABLE public.distillationprocedure OWNER TO postgres;

--
-- Name: public_electrolysiscell_cellid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_electrolysiscell_cellid_seq
    START WITH 8011
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_electrolysiscell_cellid_seq OWNER TO postgres;

--
-- Name: electrolysiscell; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.electrolysiscell (
    cellid smallint DEFAULT nextval('public.public_electrolysiscell_cellid_seq'::regclass) NOT NULL,
    systemid integer,
    cellname character varying(20),
    cellsize integer,
    isflexvolumecell boolean DEFAULT false NOT NULL,
    lockforunknowns boolean DEFAULT false NOT NULL,
    isobsolete boolean DEFAULT false NOT NULL,
    remarks character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.electrolysiscell OWNER TO postgres;

--
-- Name: public_electrolysiscellconstant_cellconstantid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_electrolysiscellconstant_cellconstantid_seq
    START WITH 89
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_electrolysiscellconstant_cellconstantid_seq OWNER TO postgres;

--
-- Name: electrolysiscellconstant; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.electrolysiscellconstant (
    cellconstantid smallint DEFAULT nextval('public.public_electrolysiscellconstant_cellconstantid_seq'::regclass) NOT NULL,
    cellid smallint DEFAULT 0 NOT NULL,
    cellconstant double precision,
    cellconstantunc double precision,
    remarks character varying(255),
    isobsolete boolean,
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50)
);


ALTER TABLE public.electrolysiscellconstant OWNER TO postgres;

--
-- Name: public_electrolysiscellrecondition_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_electrolysiscellrecondition_id_seq
    START WITH 3
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_electrolysiscellrecondition_id_seq OWNER TO postgres;

--
-- Name: electrolysiscellrecondition; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.electrolysiscellrecondition (
    id smallint DEFAULT nextval('public.public_electrolysiscellrecondition_id_seq'::regclass) NOT NULL,
    cellid smallint DEFAULT 0 NOT NULL,
    reconditiondate timestamp without time zone,
    comment character varying(1000),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50)
);


ALTER TABLE public.electrolysiscellrecondition OWNER TO postgres;

--
-- Name: public_electrolysissystem_elyssystemid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_electrolysissystem_elyssystemid_seq
    START WITH 2009
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_electrolysissystem_elyssystemid_seq OWNER TO postgres;

--
-- Name: electrolysissystem; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.electrolysissystem (
    elyssystemid integer DEFAULT nextval('public.public_electrolysissystem_elyssystemid_seq'::regclass) NOT NULL,
    samplevolume smallint,
    iscellsizeflexible boolean
);


ALTER TABLE public.electrolysissystem OWNER TO postgres;

--
-- Name: public_employee_employeeid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_employee_employeeid_seq
    START WITH 17
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_employee_employeeid_seq OWNER TO postgres;

--
-- Name: employee; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.employee (
    employeeid integer DEFAULT nextval('public.public_employee_employeeid_seq'::regclass) NOT NULL,
    firstmiddlename character varying(50),
    lastname character varying(50),
    systemloginname character varying(255),
    isobsolete boolean DEFAULT false NOT NULL,
    functionaltitle character varying(255),
    phonenumber character varying(255),
    emailaddress character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50),
    defaultjobid smallint,
    password_hash character varying(255)
);


ALTER TABLE public.employee OWNER TO postgres;

--
-- Name: COLUMN employee.password_hash; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.employee.password_hash IS 'bcrypt password hash for web API JWT authentication. NULL = account not yet activated for web access. Initial value seeded from SystemLoginName via migration.';


--
-- Name: employee_role_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.employee_role_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.employee_role_id_seq OWNER TO postgres;

--
-- Name: employee_role; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.employee_role (
    id integer DEFAULT nextval('public.employee_role_id_seq'::regclass) NOT NULL,
    employeeid integer DEFAULT 0 NOT NULL,
    roleid integer DEFAULT 0 NOT NULL,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.employee_role OWNER TO postgres;

--
-- Name: employeemessage; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.employeemessage (
    id integer NOT NULL,
    senderid integer NOT NULL,
    recipientid integer NOT NULL,
    message text NOT NULL,
    isread boolean DEFAULT false NOT NULL,
    createdatestamp timestamp without time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.employeemessage OWNER TO postgres;

--
-- Name: employeemessage_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.employeemessage ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.employeemessage_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: public_enrichmentprocedure_procedureid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_enrichmentprocedure_procedureid_seq
    START WITH 2016
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_enrichmentprocedure_procedureid_seq OWNER TO postgres;

--
-- Name: enrichmentprocedure; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.enrichmentprocedure (
    procedureid integer DEFAULT nextval('public.public_enrichmentprocedure_procedureid_seq'::regclass) NOT NULL,
    spikeid integer,
    deadwaterid integer,
    numberofspike smallint,
    numberofdeadwater smallint,
    numberoflabair smallint,
    hasdeuteriummethod boolean DEFAULT false NOT NULL,
    startingwatermass double precision,
    targetwatermass double precision,
    na2o2mass double precision,
    amperehour double precision,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.enrichmentprocedure OWNER TO postgres;

--
-- Name: equipment; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.equipment (
    equipmentid integer NOT NULL,
    categoryid smallint DEFAULT 0 NOT NULL,
    identifier character varying(1),
    equipmentname character varying(255),
    numberofpositions integer,
    manufacturername character varying(255),
    modelname character varying(255),
    serialnumber character varying(50),
    inventroyrefno character varying(50),
    startoperationdate timestamp without time zone,
    endoperationdate timestamp without time zone,
    defaultprocedureid smallint,
    analysisimportformat smallint,
    sampleexportformat smallint,
    location character varying(50),
    acquisitiondate timestamp without time zone,
    haswarranty boolean,
    warrantyendson timestamp without time zone,
    hascontract boolean,
    contractendson timestamp without time zone,
    contractrefno character varying(50),
    maintenanceintervalmonths smallint,
    maintenancealertdays smallint,
    isobsolete boolean,
    remarks character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50),
    typeid smallint
);


ALTER TABLE public.equipment OWNER TO postgres;

--
-- Name: equipment_equipmentid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.equipment ALTER COLUMN equipmentid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.equipment_equipmentid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: equipment_job_procedure; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.equipment_job_procedure (
    equipmentid integer NOT NULL,
    categoryid integer NOT NULL
);


ALTER TABLE public.equipment_job_procedure OWNER TO postgres;

--
-- Name: public_equipment_measurables_equipmentid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_equipment_measurables_equipmentid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_equipment_measurables_equipmentid_seq OWNER TO postgres;

--
-- Name: equipment_measurables; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.equipment_measurables (
    equipmentid integer DEFAULT nextval('public.public_equipment_measurables_equipmentid_seq'::regclass) NOT NULL,
    measurableid smallint DEFAULT 0 NOT NULL,
    createdatestamp timestamp without time zone
);


ALTER TABLE public.equipment_measurables OWNER TO postgres;

--
-- Name: equipment_type; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.equipment_type (
    typeid smallint NOT NULL,
    typename character varying(100) NOT NULL,
    sortorder smallint DEFAULT 0 NOT NULL
);


ALTER TABLE public.equipment_type OWNER TO postgres;

--
-- Name: equipment_type_typeid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.equipment_type ALTER COLUMN typeid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.equipment_type_typeid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: public_equipmentmaintenance_maintenanceid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_equipmentmaintenance_maintenanceid_seq
    START WITH 8
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_equipmentmaintenance_maintenanceid_seq OWNER TO postgres;

--
-- Name: equipmentmaintenance; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.equipmentmaintenance (
    maintenanceid integer DEFAULT nextval('public.public_equipmentmaintenance_maintenanceid_seq'::regclass) NOT NULL,
    equipmentid integer DEFAULT 0 NOT NULL,
    categoryid smallint DEFAULT 0 NOT NULL,
    maintenancetype smallint,
    maintenancedate timestamp without time zone,
    comments text,
    operator character varying(50),
    isrecordclosed boolean,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifuserstamp character varying(50),
    modifdatestamp timestamp without time zone
);


ALTER TABLE public.equipmentmaintenance OWNER TO postgres;

--
-- Name: public_equipmentmassspec_equipmentid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_equipmentmassspec_equipmentid_seq
    START WITH 5
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_equipmentmassspec_equipmentid_seq OWNER TO postgres;

--
-- Name: equipmentmassspec; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.equipmentmassspec (
    equipmentid integer DEFAULT nextval('public.public_equipmentmassspec_equipmentid_seq'::regclass) NOT NULL,
    identifier character varying(1) DEFAULT ''::character varying NOT NULL,
    description character varying(50),
    vendormsid character varying(50),
    b_di boolean,
    ysn_cf boolean,
    analysisimportformat character varying(50),
    sampleexportformat character varying(50),
    b_missinganalyses boolean,
    b_integerportnumbers boolean,
    b_julianworkingstd boolean,
    b_storemajorionbeams boolean,
    b_storeiongauges boolean,
    b_storeinterfmasses boolean,
    b_storecfareas boolean,
    b_storeunexpectedprocedures boolean,
    rstr_prefixmostcommon character varying(1),
    str_minamplpeak character varying(55),
    str_maxamplpeak character varying(55),
    str_h2refgas character varying(10),
    str_corefgas character varying(10),
    str_co2refgas character varying(10),
    str_n2refgas character varying(10),
    str_n2orefgas character varying(10),
    str_so2refgas character varying(10),
    str_o2refgas character varying(10),
    str_airrefgas character varying(10),
    str_gcc13cheading character varying(40),
    str_gcc15nheading character varying(40),
    str_gcc18oheading character varying(40),
    dat_stamp timestamp without time zone,
    str_ch4clrefgas character varying(10),
    int_injstoignore smallint,
    int_prepinjs smallint
);


ALTER TABLE public.equipmentmassspec OWNER TO postgres;

--
-- Name: equipmenttrayconfig; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.equipmenttrayconfig (
    equipmentid integer NOT NULL,
    traycount smallint DEFAULT 1 NOT NULL,
    trayrows smallint NOT NULL,
    traycols smallint NOT NULL,
    vialcapacity smallint GENERATED ALWAYS AS ((trayrows * traycols)) STORED
);


ALTER TABLE public.equipmenttrayconfig OWNER TO postgres;

--
-- Name: TABLE equipmenttrayconfig; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON TABLE public.equipmenttrayconfig IS 'Physical tray layout for any instrument.';


--
-- Name: COLUMN equipmenttrayconfig.traycount; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.equipmenttrayconfig.traycount IS 'Number of trays the instrument holds.';


--
-- Name: COLUMN equipmenttrayconfig.trayrows; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.equipmenttrayconfig.trayrows IS 'Rows per tray.';


--
-- Name: COLUMN equipmenttrayconfig.traycols; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.equipmenttrayconfig.traycols IS 'Columns per tray.';


--
-- Name: COLUMN equipmenttrayconfig.vialcapacity; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.equipmenttrayconfig.vialcapacity IS 'Vials per tray (trayrows × traycols, generated).';


--
-- Name: public_finalvalue_analysisid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_finalvalue_analysisid_seq
    START WITH 100601
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_finalvalue_analysisid_seq OWNER TO postgres;

--
-- Name: finalvalue; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.finalvalue (
    analysisid integer DEFAULT nextval('public.public_finalvalue_analysisid_seq'::regclass) NOT NULL,
    measurableid smallint DEFAULT 0 NOT NULL,
    measurableunit smallint,
    fvalue double precision,
    fvalueunc double precision,
    lldstatus smallint,
    rejectflag boolean DEFAULT false NOT NULL,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50)
);


ALTER TABLE public.finalvalue OWNER TO postgres;

--
-- Name: globalmemo; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.globalmemo (
    id integer NOT NULL,
    message text,
    employeeid integer,
    createdatestamp timestamp without time zone,
    modifdatestamp timestamp without time zone
);


ALTER TABLE public.globalmemo OWNER TO postgres;

--
-- Name: globalmemo_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.globalmemo ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.globalmemo_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: globalvalue; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.globalvalue (
    id integer NOT NULL,
    token character varying(50),
    tokenvalue character varying(255),
    isobsolete boolean DEFAULT false NOT NULL,
    description character varying(127),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.globalvalue OWNER TO postgres;

--
-- Name: globalvalue_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.globalvalue ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.globalvalue_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: guiequipmentmaintenancetype; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.guiequipmentmaintenancetype (
    typeid integer NOT NULL,
    maintenancetype character varying(255),
    description text
);


ALTER TABLE public.guiequipmentmaintenancetype OWNER TO postgres;

--
-- Name: guiequipmentmaintenancetype_typeid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.guiequipmentmaintenancetype ALTER COLUMN typeid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.guiequipmentmaintenancetype_typeid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: guitblelectrolysiscellconstant; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.guitblelectrolysiscellconstant (
    lscrunid integer,
    elysrunid integer,
    dtmelysrunend timestamp without time zone,
    analysisid integer,
    sampleid integer,
    sampletype integer,
    cellid integer,
    elyssystemid integer,
    equipmentid integer,
    dblwatervolumeinit double precision,
    dblwatervolumeinitunc double precision,
    dblwatervolumefinal double precision,
    dblwatervolumefinalunc double precision,
    dblpreppm double precision,
    dblpostppm double precision,
    dblpreppmunc double precision,
    dblpostppmunc double precision,
    diluentmassafter double precision,
    cellconstant double precision,
    cellconstantunc double precision,
    dbl2hrecovery double precision,
    dbl3hrecovery double precision,
    dbl2henrichmentfactor double precision,
    dbl2henrichmentfactorunc double precision,
    dbl3henrichmentfactor double precision,
    dbl3henrichmentfactorunc double precision,
    meancc double precision,
    meanccunc double precision,
    flinearfitrsqrd double precision,
    flinearfitall double precision,
    blnoutlier boolean
);


ALTER TABLE public.guitblelectrolysiscellconstant OWNER TO postgres;

--
-- Name: guitblenrichmentfactormethod; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.guitblenrichmentfactormethod (
    id integer NOT NULL,
    sname character varying(50),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50)
);


ALTER TABLE public.guitblenrichmentfactormethod OWNER TO postgres;

--
-- Name: guitblenrichmentfactormethod_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.guitblenrichmentfactormethod ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.guitblenrichmentfactormethod_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: guitblfileformat; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.guitblfileformat (
    lngformatid integer NOT NULL,
    intcategory smallint,
    strformatname character varying(50),
    strinstrumentname character varying(50),
    strinstrumentmodel character varying(50),
    isactive boolean,
    remarks character varying(255)
);


ALTER TABLE public.guitblfileformat OWNER TO postgres;

--
-- Name: guitblfileformat_lngformatid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.guitblfileformat ALTER COLUMN lngformatid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.guitblfileformat_lngformatid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: guitbllookupldl; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.guitbllookupldl (
    intid smallint,
    strdescription character varying(100)
);


ALTER TABLE public.guitbllookupldl OWNER TO postgres;

--
-- Name: guitblscintillationcocktail; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.guitblscintillationcocktail (
    lngscintillantid integer NOT NULL,
    strscintillantname character varying(50),
    strmanufacturer character varying(50),
    dtmenteredoperation timestamp without time zone,
    dtmremovedfromoperation timestamp without time zone,
    dtmcreatedatestamp timestamp without time zone,
    strcreateuserstamp character varying(50)
);


ALTER TABLE public.guitblscintillationcocktail OWNER TO postgres;

--
-- Name: guitblscintillationcocktail_lngscintillantid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.guitblscintillationcocktail ALTER COLUMN lngscintillantid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.guitblscintillationcocktail_lngscintillantid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: guitblsicorrections; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.guitblsicorrections (
    correctiontype smallint,
    correctionname character varying(255),
    ourlabid character varying(20),
    remarks character varying(255)
);


ALTER TABLE public.guitblsicorrections OWNER TO postgres;

--
-- Name: guitblsicorrectionsubtype; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.guitblsicorrectionsubtype (
    correctiontype smallint,
    correctionsubtype smallint,
    subtypename character varying(255),
    remarks character varying(255)
);


ALTER TABLE public.guitblsicorrectionsubtype OWNER TO postgres;

--
-- Name: invoice; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.invoice (
    invoiceid integer DEFAULT 0 NOT NULL,
    invoiceyear smallint,
    invoiceperyear integer,
    invoicenumber character varying(255),
    invoicedate timestamp without time zone,
    paiddate timestamp without time zone,
    ordernumber character varying(255),
    accountnumber character varying(255),
    reportid integer,
    submissionid integer,
    currencymodifier double precision,
    currencysign character varying(255),
    discount double precision,
    surcharge double precision,
    justification character varying(255),
    signingperson integer,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.invoice OWNER TO postgres;

--
-- Name: public_job_procedure_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_job_procedure_id_seq
    START WITH 15
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_job_procedure_id_seq OWNER TO postgres;

--
-- Name: job_procedure; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.job_procedure (
    id integer DEFAULT nextval('public.public_job_procedure_id_seq'::regclass) NOT NULL,
    moduleid integer,
    sname character varying(50),
    tablename character varying(50),
    fieldname character varying(50),
    procedurename character varying(50),
    run_table character varying(100),
    run_endfld character varying(50),
    loadlist_table character varying(100),
    loadlist_runfk character varying(50),
    loadlist_afield character varying(50),
    complete_statuses integer[]
);


ALTER TABLE public.job_procedure OWNER TO postgres;

--
-- Name: lab_module_config; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.lab_module_config (
    module_key character varying(20) NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    label character varying(60) NOT NULL,
    required_privilege character varying(50)
);


ALTER TABLE public.lab_module_config OWNER TO postgres;

--
-- Name: localprintersettings; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.localprintersettings (
    id integer NOT NULL,
    pc_name character varying(255),
    defaultprintername character varying(255),
    labelprintername character varying(255)
);


ALTER TABLE public.localprintersettings OWNER TO postgres;

--
-- Name: localprintersettings_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.localprintersettings ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.localprintersettings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: public_lscprocedure_procedureid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_lscprocedure_procedureid_seq
    START WITH 3010
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_lscprocedure_procedureid_seq OWNER TO postgres;

--
-- Name: lscprocedure; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.lscprocedure (
    procedureid integer DEFAULT nextval('public.public_lscprocedure_procedureid_seq'::regclass) NOT NULL,
    samplesize double precision,
    refstandardid integer,
    cocktailsize double precision,
    cocktailtype integer,
    numberofcycles integer,
    numberofcyclerepeats integer,
    cycletype integer,
    cyclelength integer,
    cyclemaxcounts integer,
    traypositionlabel character varying(255)
);


ALTER TABLE public.lscprocedure OWNER TO postgres;

--
-- Name: ngam_substance; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ngam_substance (
    substancename character varying(50) CONSTRAINT materials_materialname_not_null NOT NULL
);


ALTER TABLE public.ngam_substance OWNER TO postgres;

--
-- Name: materials; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.materials AS
 SELECT substancename AS materialname
   FROM public.ngam_substance;


ALTER VIEW public.materials OWNER TO postgres;

--
-- Name: matrix; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.matrix (
    matrixid integer NOT NULL,
    matrixname character varying(100) NOT NULL,
    matrixnote text
);


ALTER TABLE public.matrix OWNER TO postgres;

--
-- Name: matrix_matrixid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.matrix_matrixid_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.matrix_matrixid_seq OWNER TO postgres;

--
-- Name: matrix_matrixid_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.matrix_matrixid_seq OWNED BY public.matrix.matrixid;


--
-- Name: public_measurables_measurableid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_measurables_measurableid_seq
    START WITH 994
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_measurables_measurableid_seq OWNER TO postgres;

--
-- Name: measurables; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.measurables (
    measurableid integer DEFAULT nextval('public.public_measurables_measurableid_seq'::regclass) NOT NULL,
    measurablename character varying(100) DEFAULT ''::character varying NOT NULL,
    parameterlabel character varying(50),
    eductmaterial character varying(50),
    descriptionid integer,
    unitid smallint,
    isfieldparam smallint,
    halflife double precision
);


ALTER TABLE public.measurables OWNER TO postgres;

--
-- Name: public_measurementunit_unitid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_measurementunit_unitid_seq
    START WITH 5506
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_measurementunit_unitid_seq OWNER TO postgres;

--
-- Name: measurementunit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.measurementunit (
    unitid integer DEFAULT nextval('public.public_measurementunit_unitid_seq'::regclass) NOT NULL,
    shortname character varying(255) DEFAULT ''::character varying NOT NULL,
    sname character varying(255),
    multiplier double precision NOT NULL,
    "offset" double precision NOT NULL,
    unitcategory_id integer DEFAULT 0 NOT NULL,
    isdefault boolean NOT NULL,
    isbase boolean DEFAULT false NOT NULL
);


ALTER TABLE public.measurementunit OWNER TO postgres;

--
-- Name: public_media_mediaid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_media_mediaid_seq
    START WITH 1018
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_media_mediaid_seq OWNER TO postgres;

--
-- Name: media; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.media (
    mediaid smallint DEFAULT nextval('public.public_media_mediaid_seq'::regclass) NOT NULL,
    prefix character varying(1),
    medianame character varying(50),
    abbreviation character varying(10),
    createdatestamp timestamp without time zone,
    isactive boolean,
    module character varying(20)
);


ALTER TABLE public.media OWNER TO postgres;

--
-- Name: COLUMN media.module; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.media.module IS 'Analytical module key — FK (logical) to public.status_label.module';


--
-- Name: media_legacy; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.media_legacy AS
 SELECT mediaid,
    prefix,
    medianame AS sname,
    abbreviation AS shortname,
    isactive
   FROM public.media;


ALTER VIEW public.media_legacy OWNER TO postgres;

--
-- Name: module; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.module (
    id integer NOT NULL,
    modulename character(50) DEFAULT ''::bpchar NOT NULL
);


ALTER TABLE public.module OWNER TO postgres;

--
-- Name: public_networktype_networktypeid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_networktype_networktypeid_seq
    START WITH 18
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_networktype_networktypeid_seq OWNER TO postgres;

--
-- Name: networktype; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.networktype (
    networktypeid smallint DEFAULT nextval('public.public_networktype_networktypeid_seq'::regclass) NOT NULL,
    code character varying(10),
    description character varying(255)
);


ALTER TABLE public.networktype OWNER TO postgres;

--
-- Name: public_ngextractionprocedure_procedureid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_ngextractionprocedure_procedureid_seq
    START WITH 10002
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_ngextractionprocedure_procedureid_seq OWNER TO postgres;

--
-- Name: ngextractionprocedure; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ngextractionprocedure (
    procedureid integer DEFAULT nextval('public.public_ngextractionprocedure_procedureid_seq'::regclass) NOT NULL,
    firstbulbid integer,
    samplesize double precision,
    ingrowthperiodindays integer,
    degassingminutes double precision
);


ALTER TABLE public.ngextractionprocedure OWNER TO postgres;

--
-- Name: ngseqtemplate; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ngseqtemplate (
    ingseqtemplateid integer NOT NULL,
    procedureid integer,
    iinletid integer,
    iport integer,
    sampletype integer,
    ourlabid character varying(25),
    nvcinletstring character varying(100),
    bisblank boolean DEFAULT false NOT NULL,
    bisreproreference boolean DEFAULT false NOT NULL,
    bislinreference boolean DEFAULT false NOT NULL,
    nvcreferencegas character varying(50),
    freferenceamount double precision,
    istepshe integer,
    istepsne integer,
    istepsar integer,
    nvcremarks character varying(200),
    freferenceamountunc double precision
);


ALTER TABLE public.ngseqtemplate OWNER TO postgres;

--
-- Name: ngseqtemplate_ingseqtemplateid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.ngseqtemplate ALTER COLUMN ingseqtemplateid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.ngseqtemplate_ingseqtemplateid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: parameter; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.parameter AS
 SELECT DISTINCT parameterlabel AS parametername
   FROM public.measurables
  WHERE (parameterlabel IS NOT NULL)
  ORDER BY parameterlabel;


ALTER VIEW public.parameter OWNER TO postgres;

--
-- Name: phaselookup; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.phaselookup (
    phase smallint NOT NULL,
    name character varying(20) NOT NULL,
    label character varying(40) NOT NULL,
    sort_order smallint NOT NULL
);


ALTER TABLE public.phaselookup OWNER TO postgres;

--
-- Name: public_priority_priorityid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_priority_priorityid_seq
    START WITH 6
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_priority_priorityid_seq OWNER TO postgres;

--
-- Name: priority; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.priority (
    priorityid smallint DEFAULT nextval('public.public_priority_priorityid_seq'::regclass) NOT NULL,
    description character varying(255)
);


ALTER TABLE public.priority OWNER TO postgres;

--
-- Name: privilege; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.privilege (
    privilegekey text NOT NULL,
    modulekey text NOT NULL,
    isadmin boolean DEFAULT false NOT NULL,
    label text NOT NULL
);


ALTER TABLE public.privilege OWNER TO postgres;

--
-- Name: TABLE privilege; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON TABLE public.privilege IS 'Authoritative catalog of privilege keys. RolePrivilege.PrivilegeName must reference this table.';


--
-- Name: COLUMN privilege.privilegekey; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.privilege.privilegekey IS 'Lowercase key matched by check_employee_privilege(), e.g. accessams.';


--
-- Name: COLUMN privilege.modulekey; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.privilege.modulekey IS 'Module grouping used in the UI, e.g. ams, trims, siam.';


--
-- Name: COLUMN privilege.isadmin; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.privilege.isadmin IS 'True for administrative privileges, false for plain access.';


--
-- Name: COLUMN privilege.label; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.privilege.label IS 'Human-readable label shown in the Roles & Privileges UI.';


--
-- Name: processing_jobs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.processing_jobs (
    job_id integer NOT NULL,
    run_id integer NOT NULL,
    module character varying(10) DEFAULT 'SIAM'::character varying NOT NULL,
    status character varying(20) DEFAULT 'PENDING'::character varying NOT NULL,
    config_json jsonb,
    result_summary jsonb,
    error_message text,
    createdatestamp timestamp without time zone DEFAULT now(),
    modifdatestamp timestamp without time zone
);


ALTER TABLE public.processing_jobs OWNER TO postgres;

--
-- Name: processing_jobs_job_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.processing_jobs_job_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.processing_jobs_job_id_seq OWNER TO postgres;

--
-- Name: processing_jobs_job_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.processing_jobs_job_id_seq OWNED BY public.processing_jobs.job_id;


--
-- Name: protocol; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.protocol (
    protocolid integer NOT NULL,
    name character varying(255) DEFAULT ''::character varying NOT NULL,
    module character varying(50) DEFAULT ''::character varying NOT NULL,
    formatid smallint,
    description text,
    settingsjson text,
    isactive boolean DEFAULT false NOT NULL,
    isdefault boolean DEFAULT false NOT NULL,
    createdby character varying(100),
    createdat timestamp without time zone,
    modifiedby character varying(100),
    modifiedat timestamp without time zone
);


ALTER TABLE public.protocol OWNER TO postgres;

--
-- Name: protocol_protocolid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.protocol ALTER COLUMN protocolid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.protocol_protocolid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: protocolmapping; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.protocolmapping (
    mappingid integer NOT NULL,
    protocolid integer DEFAULT 0 NOT NULL,
    targetfield character varying(100) DEFAULT ''::character varying NOT NULL,
    sourceheader character varying(255) DEFAULT ''::character varying NOT NULL,
    uncertaintycolumn character varying(255),
    isnet boolean DEFAULT false NOT NULL,
    requiresbackground boolean DEFAULT false NOT NULL,
    displayorder integer DEFAULT 0 NOT NULL,
    notes text
);


ALTER TABLE public.protocolmapping OWNER TO postgres;

--
-- Name: protocolmapping_mappingid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.protocolmapping ALTER COLUMN mappingid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.protocolmapping_mappingid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: public_chemistrybatch_runid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_chemistrybatch_runid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_chemistrybatch_runid_seq OWNER TO postgres;

--
-- Name: public_chemistryloadlist_chemanalysisid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_chemistryloadlist_chemanalysisid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_chemistryloadlist_chemanalysisid_seq OWNER TO postgres;

--
-- Name: public_descriptions_descriptionid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_descriptions_descriptionid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_descriptions_descriptionid_seq OWNER TO postgres;

--
-- Name: public_dilutiondata_dilutiondataid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_dilutiondata_dilutiondataid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_dilutiondata_dilutiondataid_seq OWNER TO postgres;

--
-- Name: public_equipment_equipmentid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_equipment_equipmentid_seq
    START WITH 14002
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_equipment_equipmentid_seq OWNER TO postgres;

--
-- Name: public_globalmemo_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_globalmemo_id_seq
    START WITH 2
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_globalmemo_id_seq OWNER TO postgres;

--
-- Name: public_globalvalue_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_globalvalue_id_seq
    START WITH 100
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_globalvalue_id_seq OWNER TO postgres;

--
-- Name: public_guitblfileformat_lngformatid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_guitblfileformat_lngformatid_seq
    START WITH 18
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_guitblfileformat_lngformatid_seq OWNER TO postgres;

--
-- Name: public_localprintersettings_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_localprintersettings_id_seq
    START WITH 3
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_localprintersettings_id_seq OWNER TO postgres;

--
-- Name: public_ngseqtemplate_ingseqtemplateid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_ngseqtemplate_ingseqtemplateid_seq
    START WITH 60
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_ngseqtemplate_ingseqtemplateid_seq OWNER TO postgres;

--
-- Name: public_protocol_protocolid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_protocol_protocolid_seq
    START WITH 7
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_protocol_protocolid_seq OWNER TO postgres;

--
-- Name: public_protocolmapping_mappingid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_protocolmapping_mappingid_seq
    START WITH 426
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_protocolmapping_mappingid_seq OWNER TO postgres;

--
-- Name: public_referencecontrol_referenceid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_referencecontrol_referenceid_seq
    START WITH 3163
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_referencecontrol_referenceid_seq OWNER TO postgres;

--
-- Name: public_referencecontroldata_referencedataid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_referencecontroldata_referencedataid_seq
    START WITH 181
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_referencecontroldata_referencedataid_seq OWNER TO postgres;

--
-- Name: public_reporting_reportid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_reporting_reportid_seq
    START WITH 8
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_reporting_reportid_seq OWNER TO postgres;

--
-- Name: public_role_roleid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_role_roleid_seq
    START WITH 13
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_role_roleid_seq OWNER TO postgres;

--
-- Name: public_runprotocolsnapshot_snapshotid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_runprotocolsnapshot_snapshotid_seq
    START WITH 15
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_runprotocolsnapshot_snapshotid_seq OWNER TO postgres;

--
-- Name: public_sample_fielddata_sampleid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_sample_fielddata_sampleid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_sample_fielddata_sampleid_seq OWNER TO postgres;

--
-- Name: public_sample_sampleid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_sample_sampleid_seq
    START WITH 100609
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_sample_sampleid_seq OWNER TO postgres;

--
-- Name: public_sampletba_sampletba_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_sampletba_sampletba_id_seq
    START WITH 1943
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_sampletba_sampletba_id_seq OWNER TO postgres;

--
-- Name: public_samplingstation_stationid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_samplingstation_stationid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_samplingstation_stationid_seq OWNER TO postgres;

--
-- Name: public_samplingstationworkflow_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_samplingstationworkflow_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_samplingstationworkflow_id_seq OWNER TO postgres;

--
-- Name: public_siprocedure_procedureid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_siprocedure_procedureid_seq
    START WITH 5015
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_siprocedure_procedureid_seq OWNER TO postgres;

--
-- Name: public_station_stationid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_station_stationid_seq
    START WITH 2320
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_station_stationid_seq OWNER TO postgres;

--
-- Name: public_stationmetadataiaea_stationid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_stationmetadataiaea_stationid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_stationmetadataiaea_stationid_seq OWNER TO postgres;

--
-- Name: public_stationstatushistory_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_stationstatushistory_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_stationstatushistory_id_seq OWNER TO postgres;

--
-- Name: public_statuslookup_status_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_statuslookup_status_seq
    START WITH 1000
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_statuslookup_status_seq OWNER TO postgres;

--
-- Name: public_storelocation_storelocationid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_storelocation_storelocationid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_storelocation_storelocationid_seq OWNER TO postgres;

--
-- Name: public_submission_submissionid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_submission_submissionid_seq
    START WITH 10077
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_submission_submissionid_seq OWNER TO postgres;

--
-- Name: public_tblstation_lngstationautoid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_tblstation_lngstationautoid_seq
    START WITH 2320
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_tblstation_lngstationautoid_seq OWNER TO postgres;

--
-- Name: public_workflowjob_workflowjobid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.public_workflowjob_workflowjobid_seq
    START WITH 86
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.public_workflowjob_workflowjobid_seq OWNER TO postgres;

--
-- Name: reference_source_samples; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.reference_source_samples (
    sampleid integer NOT NULL,
    prefix character varying(1) NOT NULL,
    notes text,
    createdat timestamp without time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.reference_source_samples OWNER TO postgres;

--
-- Name: referencecontrol; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.referencecontrol (
    referenceid integer DEFAULT nextval('public.public_referencecontrol_referenceid_seq'::regclass) NOT NULL,
    description character varying(50),
    prefix character varying(1) DEFAULT ''::character varying NOT NULL,
    sampleid integer DEFAULT 0 NOT NULL,
    sampletype smallint DEFAULT 0 NOT NULL,
    availabledateto timestamp without time zone,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50),
    availabledatefrom timestamp without time zone
);


ALTER TABLE public.referencecontrol OWNER TO postgres;

--
-- Name: referencecontroldata; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.referencecontroldata (
    referencedataid integer DEFAULT nextval('public.public_referencecontroldata_referencedataid_seq'::regclass) NOT NULL,
    referenceid integer DEFAULT 0 NOT NULL,
    measurableid integer DEFAULT 0 NOT NULL,
    parameter character varying(50),
    referencedate timestamp without time zone,
    availabledatefrom timestamp without time zone,
    availabledateto timestamp without time zone,
    certifiedvalue double precision,
    certifiedvalueunc double precision,
    unitid integer,
    source character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.referencecontroldata OWNER TO postgres;

--
-- Name: reporting; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.reporting (
    reportid integer DEFAULT nextval('public.public_reporting_reportid_seq'::regclass) NOT NULL,
    reporttemplateid integer,
    isobsolete boolean,
    reportingdate timestamp without time zone,
    reportnumber character varying(255),
    yearofissuance smallint,
    numberperyear integer,
    revisionnumber smallint,
    revisioncomment character varying(255),
    contactperson integer,
    submissionid integer,
    hassamplinginformation boolean,
    hasanalyticaldetails boolean,
    hasremarkdetails boolean,
    mediaid smallint,
    reportingunit smallint,
    sigmafactor double precision,
    belowdetectionlimitlogic smallint,
    effects text,
    noncompliance text,
    opinion text,
    remarks text,
    signingperson integer,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.reporting OWNER TO postgres;

--
-- Name: role; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.role (
    roleid integer DEFAULT nextval('public.public_role_roleid_seq'::regclass) NOT NULL,
    rolename character varying(50),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50),
    canvalidate boolean DEFAULT false NOT NULL,
    validatoradmin boolean DEFAULT false NOT NULL
);


ALTER TABLE public.role OWNER TO postgres;

--
-- Name: role_module_permission; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.role_module_permission (
    roleid integer NOT NULL,
    moduleid integer NOT NULL
);


ALTER TABLE public.role_module_permission OWNER TO postgres;

--
-- Name: roleprivilege; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.roleprivilege (
    roleid integer NOT NULL,
    privilegename text NOT NULL
);


ALTER TABLE public.roleprivilege OWNER TO postgres;

--
-- Name: TABLE roleprivilege; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON TABLE public.roleprivilege IS 'Named privilege grants for roles — schema-change-free alternative to boolean columns on Role.';


--
-- Name: COLUMN roleprivilege.privilegename; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.roleprivilege.privilegename IS 'Lowercase privilege key, e.g. accessams, amsadmin. Matched case-insensitively by check_employee_privilege().';


--
-- Name: runprotocolsnapshot; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.runprotocolsnapshot (
    snapshotid integer NOT NULL,
    protocolid integer,
    module character varying(50) DEFAULT ''::character varying NOT NULL,
    runid integer DEFAULT 0 NOT NULL,
    protocolsnapshot text DEFAULT ''::text NOT NULL,
    wasmodified boolean DEFAULT false NOT NULL,
    appliedby character varying(100),
    appliedat timestamp without time zone
);


ALTER TABLE public.runprotocolsnapshot OWNER TO postgres;

--
-- Name: runprotocolsnapshot_snapshotid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.runprotocolsnapshot ALTER COLUMN snapshotid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.runprotocolsnapshot_snapshotid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sample; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.sample (
    sampleid integer DEFAULT nextval('public.public_sample_sampleid_seq'::regclass) NOT NULL,
    prefix character varying(1) DEFAULT ''::character varying NOT NULL,
    submissionid integer DEFAULT 0 NOT NULL,
    duplicatesample boolean,
    samplingsiteid integer,
    sampletype smallint,
    mediaid smallint,
    sname character varying(70) DEFAULT ''::character varying NOT NULL,
    samplevolume double precision,
    countrycode character varying(2),
    latitude double precision,
    longitude double precision,
    elevation double precision,
    collectiondate timestamp without time zone DEFAULT now() NOT NULL,
    collectionenddate timestamp without time zone,
    workflowid smallint,
    status smallint,
    samplestateuponarrival character varying(255),
    isosamplingreference character varying(255),
    isosamplingconditions character varying(255),
    isosamplingdeviations character varying(255),
    physicalmediastatus smallint,
    importedfromfile character varying(255),
    remarks character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50),
    ng_container_type smallint,
    container_type smallint
);


ALTER TABLE public.sample OWNER TO postgres;

--
-- Name: COLUMN sample.ng_container_type; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.sample.ng_container_type IS 'Noble-gas sample container type (FK → public.container_type_lookup). Set at sample registration; used by the NGAM extraction pipeline to select the correct water-mass formula in build_extraction_info().';


--
-- Name: COLUMN sample.container_type; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.sample.container_type IS 'Noble-gas sample container type (FK → public.container_type_lookup). Set at sample registration; used by the NGAM extraction pipeline to select the correct water-mass formula in build_extraction_info().';


--
-- Name: sample_duplicate_link; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.sample_duplicate_link (
    duplicate_sampleid integer NOT NULL,
    duplicate_prefix character varying(1) NOT NULL,
    parent_sampleid integer NOT NULL,
    parent_prefix character varying(1) NOT NULL,
    createdatestamp timestamp without time zone DEFAULT now() NOT NULL,
    createuserstamp character varying(50)
);


ALTER TABLE public.sample_duplicate_link OWNER TO postgres;

--
-- Name: TABLE sample_duplicate_link; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON TABLE public.sample_duplicate_link IS 'Links a duplicate sample to its primary (original) sample.  Sparse: only rows for actual pairs.';


--
-- Name: sample_fielddata; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.sample_fielddata (
    sampleid integer DEFAULT nextval('public.public_sample_fielddata_sampleid_seq'::regclass) NOT NULL,
    prefix character varying(1) DEFAULT ''::character varying NOT NULL,
    measurableid integer DEFAULT 0 NOT NULL,
    fieldvalue double precision NOT NULL
);


ALTER TABLE public.sample_fielddata OWNER TO postgres;

--
-- Name: sample_queue; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.sample_queue (
    queue_id integer NOT NULL,
    sampleid integer NOT NULL,
    prefix character varying(1) NOT NULL,
    workflowjobid integer NOT NULL,
    mediaid integer,
    priorityid integer,
    source_aid integer,
    repeat_count smallint DEFAULT 1 NOT NULL,
    queued_at timestamp without time zone DEFAULT now() NOT NULL,
    queued_by character varying(50)
);


ALTER TABLE public.sample_queue OWNER TO postgres;

--
-- Name: sample_queue_queue_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.sample_queue_queue_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.sample_queue_queue_id_seq OWNER TO postgres;

--
-- Name: sample_queue_queue_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.sample_queue_queue_id_seq OWNED BY public.sample_queue.queue_id;


--
-- Name: samplearchive; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.samplearchive (
    prefix character varying(1) NOT NULL,
    sampleid integer DEFAULT 0 NOT NULL,
    statusid integer DEFAULT 0 NOT NULL,
    available smallint,
    rebottled smallint,
    storelocation integer,
    datestamp timestamp without time zone,
    userstamp character varying(50)
);


ALTER TABLE public.samplearchive OWNER TO postgres;

--
-- Name: sampletba; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.sampletba (
    sampletba_id integer NOT NULL,
    sampleid integer DEFAULT 0 NOT NULL,
    prefix character varying(1) DEFAULT ''::character varying NOT NULL,
    mediaid smallint DEFAULT 0 NOT NULL,
    precursorid integer,
    analysisid integer,
    repeat smallint,
    workflowid smallint,
    workflowjobid smallint,
    priorityid smallint,
    equipmentid integer,
    employeeid integer,
    status smallint,
    remarks character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50)
);


ALTER TABLE public.sampletba OWNER TO postgres;

--
-- Name: sampletba_sampletba_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.sampletba ALTER COLUMN sampletba_id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.sampletba_sampletba_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: samplingstation; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.samplingstation (
    stationid integer NOT NULL,
    stationname character varying(100) DEFAULT ''::character varying NOT NULL,
    stationshortname character varying(20) DEFAULT ''::character varying NOT NULL,
    stationstatus integer DEFAULT 0 NOT NULL,
    countrycode character varying(2) DEFAULT ''::character varying NOT NULL,
    wmocode character varying(20),
    latitude double precision,
    longitude double precision,
    elevation double precision,
    remarks character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.samplingstation OWNER TO postgres;

--
-- Name: samplingstation_stationid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.samplingstation ALTER COLUMN stationid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.samplingstation_stationid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: samplingstationworkflow; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.samplingstationworkflow (
    id integer NOT NULL,
    stationid integer DEFAULT 0 NOT NULL,
    mediacode integer DEFAULT 0 NOT NULL,
    workflowid integer DEFAULT 0 NOT NULL,
    altworkflowid integer,
    analysispriority integer,
    samplingfrequency integer,
    defaultstorelocation integer
);


ALTER TABLE public.samplingstationworkflow OWNER TO postgres;

--
-- Name: samplingstationworkflow_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.samplingstationworkflow ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.samplingstationworkflow_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: siam_sianalysiscorrection_sianalysisrunid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.siam_sianalysiscorrection_sianalysisrunid_seq
    START WITH 20002
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.siam_sianalysiscorrection_sianalysisrunid_seq OWNER TO postgres;

--
-- Name: siam_sianalysiscorrectionfit_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.siam_sianalysiscorrectionfit_id_seq
    START WITH 159
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.siam_sianalysiscorrectionfit_id_seq OWNER TO postgres;

--
-- Name: siam_sianalysiscorrectionfitinj_sianalysisrunid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.siam_sianalysiscorrectionfitinj_sianalysisrunid_seq
    START WITH 20002
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.siam_sianalysiscorrectionfitinj_sianalysisrunid_seq OWNER TO postgres;

--
-- Name: siam_sianalysisinjectiondata_sianalysisid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.siam_sianalysisinjectiondata_sianalysisid_seq
    START WITH 3786
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.siam_sianalysisinjectiondata_sianalysisid_seq OWNER TO postgres;

--
-- Name: siam_sianalysisinterimdata_sianalysisdataid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.siam_sianalysisinterimdata_sianalysisdataid_seq
    START WITH 56406
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.siam_sianalysisinterimdata_sianalysisdataid_seq OWNER TO postgres;

--
-- Name: siam_sianalysisloadlist_sianalysisid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.siam_sianalysisloadlist_sianalysisid_seq
    START WITH 3999
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.siam_sianalysisloadlist_sianalysisid_seq OWNER TO postgres;

--
-- Name: siam_sianalysisrawdata_sianalysisdataid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.siam_sianalysisrawdata_sianalysisdataid_seq
    START WITH 96808
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.siam_sianalysisrawdata_sianalysisdataid_seq OWNER TO postgres;

--
-- Name: siam_sianalysisresult_sianalysisid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.siam_sianalysisresult_sianalysisid_seq
    START WITH 3786
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.siam_sianalysisresult_sianalysisid_seq OWNER TO postgres;

--
-- Name: siam_sianalysisrun_sianalysisrunid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.siam_sianalysisrun_sianalysisrunid_seq
    START WITH 50002
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.siam_sianalysisrun_sianalysisrunid_seq OWNER TO postgres;

--
-- Name: siam_siinlets_siid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.siam_siinlets_siid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.siam_siinlets_siid_seq OWNER TO postgres;

--
-- Name: siam_simeasurement_siid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.siam_simeasurement_siid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.siam_simeasurement_siid_seq OWNER TO postgres;

--
-- Name: siprocedure; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.siprocedure (
    procedureid integer DEFAULT nextval('public.public_siprocedure_procedureid_seq'::regclass) NOT NULL,
    description character varying(50),
    enrichedsample smallint,
    b_printstbalistonly boolean,
    str_prefixemptyports character varying(1),
    lng_sampleemptyports integer,
    str_prefixfloatingref character varying(1),
    lng_samplefloatingref integer,
    int_numberfloatingref smallint,
    method character varying(32),
    methodalt character varying(32),
    b_ea_styletemplate boolean,
    int_samplerowsperport smallint,
    b_printalternativesamplelist boolean,
    randomizesamples boolean,
    randomizecontrols boolean,
    injections smallint,
    prepinjections smallint,
    ignoredinjections smallint,
    isobsolete boolean,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.siprocedure OWNER TO postgres;

--
-- Name: station; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.station (
    stationid integer NOT NULL,
    name character varying(100) DEFAULT ''::character varying NOT NULL,
    shortname character varying(50) DEFAULT ''::character varying NOT NULL,
    statusid integer DEFAULT 0 NOT NULL,
    countrycode character varying(2),
    wmocode character varying(20),
    networktypeid smallint,
    latitude double precision,
    longitude double precision,
    elevation double precision,
    comments text,
    labstableisotopeid integer,
    labtritiumid integer,
    samplingfrequencysi smallint,
    samplingfrequencytr smallint,
    excludefromisoscape boolean,
    climatezoneid integer,
    createdat timestamp without time zone
);


ALTER TABLE public.station OWNER TO postgres;

--
-- Name: station_stationid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.station ALTER COLUMN stationid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.station_stationid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: stationmetadataiaea; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.stationmetadataiaea (
    stationid integer DEFAULT nextval('public.public_stationmetadataiaea_stationid_seq'::regclass) NOT NULL,
    networktype integer,
    samplesiteid integer,
    wmocode integer,
    submitterid integer,
    managerid integer,
    nationalnetwork integer,
    excludefromisoscape smallint,
    excludereason character varying(255),
    defaultsamplingmethod integer,
    reportingschedule integer,
    climatezone integer,
    comments character varying(500)
);


ALTER TABLE public.stationmetadataiaea OWNER TO postgres;

--
-- Name: stationnetworktype; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.stationnetworktype (
    networktype smallint NOT NULL,
    networktypeletter character varying(1),
    networktypedesc character varying(255)
);


ALTER TABLE public.stationnetworktype OWNER TO postgres;

--
-- Name: stationstatus; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.stationstatus (
    statusid smallint DEFAULT 0 NOT NULL,
    statusdescription character varying(255)
);


ALTER TABLE public.stationstatus OWNER TO postgres;

--
-- Name: stationstatushistory; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.stationstatushistory (
    id integer NOT NULL,
    stationid integer DEFAULT 0 NOT NULL,
    statusid integer,
    datestamp timestamp without time zone,
    userstamp character varying(50)
);


ALTER TABLE public.stationstatushistory OWNER TO postgres;

--
-- Name: stationstatushistory_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.stationstatushistory ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.stationstatushistory_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: status_label; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.status_label (
    module character varying(20) NOT NULL,
    status smallint NOT NULL,
    label character varying(100) NOT NULL
);


ALTER TABLE public.status_label OWNER TO postgres;

--
-- Name: TABLE status_label; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON TABLE public.status_label IS 'Per-module status label overrides; falls back to statuslookup.description';


--
-- Name: COLUMN status_label.module; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.status_label.module IS 'Module key: TRIMS | SIAM | NGAM | NGAM_Ingrowth | NGAM_Seq | SAMPLE | SUBMISSION';


--
-- Name: COLUMN status_label.status; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.status_label.status IS 'FK → public.statuslookup.status';


--
-- Name: COLUMN status_label.label; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.status_label.label IS 'Short human-readable label for this module + status combination';


--
-- Name: statuslookup; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.statuslookup (
    status smallint DEFAULT nextval('public.public_statuslookup_status_seq'::regclass) NOT NULL,
    description character varying(255)
);


ALTER TABLE public.statuslookup OWNER TO postgres;

--
-- Name: storelocation; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.storelocation (
    storelocationid integer NOT NULL,
    storelocationdesc character varying(50) DEFAULT ''::character varying NOT NULL
);


ALTER TABLE public.storelocation OWNER TO postgres;

--
-- Name: storelocation_storelocationid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.storelocation ALTER COLUMN storelocationid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.storelocation_storelocationid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: submission; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.submission (
    submissionid integer DEFAULT nextval('public.public_submission_submissionid_seq'::regclass) NOT NULL,
    submissiontype smallint DEFAULT 0 NOT NULL,
    technicalofficer integer,
    customerid integer,
    mediaid smallint,
    submissionname character varying(255) DEFAULT ''::character varying NOT NULL,
    submissionsite character varying(255),
    storelocation character varying(255),
    submissiondate timestamp without time zone,
    reporteddate timestamp without time zone,
    payerid integer,
    payerreference character varying(255),
    invoiceid integer,
    invoicedate timestamp without time zone,
    priorityid smallint,
    receivingno integer,
    requestedworkflow integer,
    status smallint,
    remarks character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.submission OWNER TO postgres;

--
-- Name: tblmatchstationstatus; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tblmatchstationstatus (
    lngstationstatusid integer NOT NULL,
    lngstationautoid integer,
    intstatusid smallint,
    dtmtimestamp timestamp without time zone,
    struserstamp character varying(255)
);


ALTER TABLE public.tblmatchstationstatus OWNER TO postgres;

--
-- Name: tblmatchstationstatus_lngstationstatusid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.tblmatchstationstatus ALTER COLUMN lngstationstatusid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.tblmatchstationstatus_lngstationstatusid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: tblnetworktype; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tblnetworktype (
    intnetworktype smallint,
    strnetworktypeletter character varying(1),
    strnetworktypedesc character varying(255)
);


ALTER TABLE public.tblnetworktype OWNER TO postgres;

--
-- Name: tblreporttemplate; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tblreporttemplate (
    reporttemplateid integer DEFAULT 0 NOT NULL,
    strtemplatename character varying(255),
    isobsolete boolean,
    hassamplinginformation boolean,
    hasanalyticsdetails boolean,
    hasremarkdetails boolean,
    strlogopath character varying(255),
    strreporttitle character varying(255),
    strreportsubtitle character varying(255),
    intreportnumberformat smallint,
    strreportnumberformat character varying(255),
    strreportnumbercaption character varying(255),
    strdateissuedcaption character varying(255),
    strrevisioncommentcaption character varying(255),
    strpages1caption character varying(255),
    strpages2caption character varying(255),
    strlabaddressline1 character varying(255),
    strlabaddressline2 character varying(255),
    strlabaddressline3 character varying(255),
    strlabaddressline4 character varying(255),
    strlabaddressline5 character varying(255),
    strlabaddressline6 character varying(255),
    contactperson integer,
    strcontactpersoncaption character varying(255),
    strsubmissioninfoheader character varying(255),
    strclientinfoheader character varying(255),
    strprojectinfocaption character varying(255),
    strprojectidcaption character varying(255),
    strprojectnamecaption character varying(255),
    strassaytypecaption character varying(255),
    strnumberofsamplescaption character varying(255),
    strrequestedworkflowcaption character varying(255),
    strsubmissiondatecaption character varying(255),
    strprojectmanagercaption character varying(255),
    strbatchnumbercaption character varying(255),
    strsamplinginfoheader character varying(255),
    strsamplingexternaltext character varying(255),
    strsamplinglabidheader character varying(255),
    strsamplingfieldidheader character varying(255),
    strsamplinglatitudeheader character varying(255),
    strsamplinglongitudeheader character varying(255),
    strsamplingdateheader character varying(255),
    strsamplingenddateheader character varying(255),
    strsamplingplanreferenceheader character varying(255),
    strsamplingconditionheader character varying(255),
    strsamplingremarkheader character varying(255),
    stranalyticalmethodsheader character varying(255),
    stranalyticspreparationcaption character varying(255),
    stranalyticsenrichmentcaption character varying(255),
    stranalyticscountingcaption character varying(255),
    stranalyticspostprocesscaption character varying(255),
    strresultsheader character varying(255),
    strresultlabidheader character varying(255),
    strresultfieldidheader character varying(255),
    strresultsampleconditionheader character varying(255),
    strresultprocedurecodeheader character varying(255),
    intresultunit smallint,
    dblresultsigmafactor double precision,
    belowdetectionlimitlogic smallint,
    strtextbelowdl character varying(255),
    strfootnootetext character varying(255),
    stradditionalinfoheader character varying(255),
    strdeviationsheader character varying(255),
    strnoncomplianceheader character varying(255),
    stropinionsheader character varying(255),
    remarksheader character varying(255),
    strclosingtext character varying(255),
    strfooterpageoftext character varying(255),
    strunsignedtext character varying(255),
    signingperson integer,
    strfooterpagetext character varying(255),
    strfooterline1 character varying(255),
    strfooterline2 character varying(255),
    strfooterline3 character varying(255),
    strfooterline4 character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.tblreporttemplate OWNER TO postgres;

--
-- Name: tblsampletype; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tblsampletype (
    strprefix character varying(1),
    intsampletype smallint DEFAULT 0 NOT NULL,
    mediacode smallint,
    strshortdescription character varying(5),
    strlongdescription character varying(30)
);


ALTER TABLE public.tblsampletype OWNER TO postgres;

--
-- Name: tblstation; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tblstation (
    lngstationautoid integer DEFAULT nextval('public.public_tblstation_lngstationautoid_seq'::regclass) NOT NULL,
    intnetworktype smallint,
    lngsamplesiteid integer,
    strcountrycode character varying(3),
    strstationname character varying(255),
    strstationshortname character varying(20),
    strstationlimsname character varying(10),
    strwmocode character varying(8),
    lngmaincounterpartid integer,
    dbllatitude double precision,
    dbllongitude double precision,
    dblelevation double precision,
    dblmaplatitude double precision,
    dblmaplongitude double precision,
    lnglabstableisotope integer,
    lnglabtritium integer,
    lngdataproviderprecipitation integer,
    lngdataproviderairtemp integer,
    lngdataprovicdervporrh integer,
    memcomments text,
    lngnationalnetwork integer,
    intsamplingfrequencysi smallint,
    intsamplingfrequencytr smallint,
    blnintercomparisonsi boolean,
    blnintercomparisontr boolean,
    lnglabicsi integer,
    lnglabictr integer,
    lngisohisprojectid integer,
    strlimsprojecttext character varying(10),
    intcustomsranking smallint,
    lngtrimssubmittedid integer,
    lnglimssubmitterid integer,
    lngtrimsmanagerid integer,
    inttrimspriority smallint,
    inttrimsmainworkflow smallint,
    inttrimsaltworkflow smallint,
    lngtrimsdefaultstorelocation integer,
    lngdefaultanalysismethodsi integer,
    lngdefaultanalysismethodtr integer,
    lngdefaultanalysismethodpp integer,
    lngdefaultanalysismethodat integer,
    lngdefaultanalysismethodvp integer,
    lngicmethodsi integer,
    lngicmethodtr integer,
    dbldexthreshold double precision,
    lngdefaultsamplingmethod integer,
    inttrimslowvolumehandling smallint,
    intreportingschedule integer,
    blnexcludefromisoscape boolean,
    strexcludereason character varying(255),
    lngclimatezone integer,
    strghcn4id character varying(255),
    lngghcn2id integer,
    intghcn2suffix smallint,
    strnewisohissiteuid character varying(255),
    strnewisohisprojectnumber character varying(255),
    lngnewprojectid integer,
    lngsamplesitetype integer,
    lngwmoregionid integer,
    lngcoordinatemeasmethod integer,
    strsiteclientmatchtag character varying(255),
    lngtimezone1 integer,
    inttimezone1from smallint,
    inttimezone1to smallint,
    lngtimezone2 integer,
    inttimezone2from smallint,
    inttimezone2to smallint,
    lngtimezone3 integer,
    inttimezone3from smallint,
    inttimezone3to smallint,
    lngdefaultsampletype integer
);


ALTER TABLE public.tblstation OWNER TO postgres;

--
-- Name: tblstationstatus; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tblstationstatus (
    intstatusid smallint,
    strstatusdescription character varying(255)
);


ALTER TABLE public.tblstationstatus OWNER TO postgres;

--
-- Name: tblstorelocations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tblstorelocations (
    lngstorelocationid integer NOT NULL,
    strstorelocationdesc character varying(255)
);


ALTER TABLE public.tblstorelocations OWNER TO postgres;

--
-- Name: tblstorelocations_lngstorelocationid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.tblstorelocations ALTER COLUMN lngstorelocationid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.tblstorelocations_lngstorelocationid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: templatemetadata; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.templatemetadata (
    templateid integer NOT NULL,
    procedureid integer NOT NULL,
    templatename character varying(50) NOT NULL,
    modulecode character varying(10),
    description character varying(255),
    createdatestamp timestamp without time zone DEFAULT now(),
    createuserstamp character varying(50)
);


ALTER TABLE public.templatemetadata OWNER TO postgres;

--
-- Name: templatemetadata_templateid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.templatemetadata_templateid_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.templatemetadata_templateid_seq OWNER TO postgres;

--
-- Name: templatemetadata_templateid_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.templatemetadata_templateid_seq OWNED BY public.templatemetadata.templateid;


--
-- Name: trims_deuteriumenrichment_deuteriumid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_deuteriumenrichment_deuteriumid_seq
    START WITH 1221
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_deuteriumenrichment_deuteriumid_seq OWNER TO postgres;

--
-- Name: trims_electrolysis_electrolysisid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_electrolysis_electrolysisid_seq
    START WITH 223
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_electrolysis_electrolysisid_seq OWNER TO postgres;

--
-- Name: trims_electrolysisrun_runid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_electrolysisrun_runid_seq
    START WITH 8110
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_electrolysisrun_runid_seq OWNER TO postgres;

--
-- Name: trims_guitblimportmapping_mappingid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_guitblimportmapping_mappingid_seq
    START WITH 490
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_guitblimportmapping_mappingid_seq OWNER TO postgres;

--
-- Name: trims_lscloadlist_countid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_lscloadlist_countid_seq
    START WITH 300
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_lscloadlist_countid_seq OWNER TO postgres;

--
-- Name: trims_lscprocedureprotocol_procedureprotocolid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_lscprocedureprotocol_procedureprotocolid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_lscprocedureprotocol_procedureprotocolid_seq OWNER TO postgres;

--
-- Name: trims_lscprotocol_protocolid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_lscprotocol_protocolid_seq
    START WITH 6
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_lscprotocol_protocolid_seq OWNER TO postgres;

--
-- Name: trims_lscprotocolmapping_mappingid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_lscprotocolmapping_mappingid_seq
    START WITH 35
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_lscprotocolmapping_mappingid_seq OWNER TO postgres;

--
-- Name: trims_lscprotocolsettings_settingid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_lscprotocolsettings_settingid_seq
    START WITH 13
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_lscprotocolsettings_settingid_seq OWNER TO postgres;

--
-- Name: trims_lscresult_lscresultid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_lscresult_lscresultid_seq
    START WITH 124
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_lscresult_lscresultid_seq OWNER TO postgres;

--
-- Name: trims_lscrun_runid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_lscrun_runid_seq
    START WITH 10085
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_lscrun_runid_seq OWNER TO postgres;

--
-- Name: trims_lscrunmean_countid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_lscrunmean_countid_seq
    START WITH 300
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_lscrunmean_countid_seq OWNER TO postgres;

--
-- Name: trims_lscrunprotocol_runprotocolid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_lscrunprotocol_runprotocolid_seq
    START WITH 10
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_lscrunprotocol_runprotocolid_seq OWNER TO postgres;

--
-- Name: trims_lscrunraw_countrawid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_lscrunraw_countrawid_seq
    START WITH 177904
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_lscrunraw_countrawid_seq OWNER TO postgres;

--
-- Name: trims_primarydistillation_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_primarydistillation_id_seq
    START WITH 10313
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_primarydistillation_id_seq OWNER TO postgres;

--
-- Name: trims_primarydistillationbatch_runid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_primarydistillationbatch_runid_seq
    START WITH 10030
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_primarydistillationbatch_runid_seq OWNER TO postgres;

--
-- Name: trims_primarydistillationdata_distillationdataid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.trims_primarydistillationdata_distillationdataid_seq
    START WITH 771
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.trims_primarydistillationdata_distillationdataid_seq OWNER TO postgres;

--
-- Name: validation_log; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.validation_log (
    logid integer NOT NULL,
    analysisid integer NOT NULL,
    runid integer,
    module character varying(10) NOT NULL,
    action character varying(20) NOT NULL,
    metric_name character varying(30),
    metric_value double precision,
    threshold_value double precision,
    passed boolean,
    override_reason text,
    employeeid integer,
    signatoryname character varying(100),
    signaturetime timestamp without time zone DEFAULT now() NOT NULL,
    createdatestamp timestamp without time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.validation_log OWNER TO postgres;

--
-- Name: validation_log_logid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.validation_log ALTER COLUMN logid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.validation_log_logid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: vw_final_results; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.vw_final_results AS
 SELECT f.analysisid,
    f.measurableid,
    f.measurableunit,
    f.fvalue,
    f.fvalueunc,
    f.lldstatus,
    f.rejectflag
   FROM public.finalvalue f
  WHERE (NOT (EXISTS ( SELECT 1
           FROM ngam.ngpreparations p
          WHERE (p.analysisid = f.analysisid))))
UNION ALL
 SELECT p.analysisid,
    an.analyteid AS measurableid,
    an.unitid AS measurableunit,
    be.fccstppergram AS fvalue,
    be.fccstppergramuncertainty AS fvalueunc,
    NULL::smallint AS lldstatus,
    false AS rejectflag
   FROM (((((ngam.ngpreparations p
     JOIN ngam.ngheaders h ON ((h.ipreparationid = p.inoblepreparationid)))
     JOIN ngam.ngblock b ON ((b.iheaderid = h.inobleheaderid)))
     JOIN ngam.ngblockevaluation be ON ((be.iblockid = b.iblockid)))
     JOIN public.analytes an ON ((lower((an.parameterlabel)::text) = lower((b.nvcname)::text))))
     JOIN public.analysis a ON ((a.analysisid = p.analysisid)))
  WHERE ((a.sampleid IS NOT NULL) AND (a.status >= 8) AND (be.fccstppergram IS NOT NULL))
UNION ALL
 SELECT p.analysisid,
    an.analyteid AS measurableid,
    NULL::integer AS measurableunit,
    rr.drift_corrected AS fvalue,
    rr.drift_corrected_unc AS fvalueunc,
    NULL::smallint AS lldstatus,
    false AS rejectflag
   FROM (((ngam.ngratioresult rr
     JOIN ngam.ngpreparations p ON (((p.runid = rr.runid) AND (p.positioninrun = rr.positioninrun))))
     JOIN public.analytes an ON ((lower((an.parameterlabel)::text) = lower(replace(rr.ratio_name, '/'::text, ''::text)))))
     JOIN public.analysis a ON ((a.analysisid = p.analysisid)))
  WHERE ((a.sampleid IS NOT NULL) AND (a.status >= 8) AND (rr.drift_corrected IS NOT NULL));


ALTER VIEW public.vw_final_results OWNER TO postgres;

--
-- Name: VIEW vw_final_results; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON VIEW public.vw_final_results IS 'Unified reporting view: SIAM/TRIMS/3He from public.finalvalue; NGAM SMS/QMS from ngam.ngblockevaluation + ngam.ngratioresult.';


--
-- Name: vwcellconstantslatest; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.vwcellconstantslatest AS
 SELECT q.cellid,
    ec.modifdatestamp,
    ec.cellconstant,
    ec.cellconstantunc
   FROM (( SELECT electrolysiscell.cellid,
            max(electrolysiscellconstant.modifdatestamp) AS maxofdtmmodifdatestamp
           FROM (public.electrolysiscell
             LEFT JOIN public.electrolysiscellconstant ON ((electrolysiscell.cellid = electrolysiscellconstant.cellid)))
          WHERE (electrolysiscellconstant.isobsolete IS FALSE)
          GROUP BY electrolysiscell.cellid) q
     LEFT JOIN public.electrolysiscellconstant ec ON (((q.maxofdtmmodifdatestamp = ec.modifdatestamp) AND (q.cellid = ec.cellid))));


ALTER VIEW public.vwcellconstantslatest OWNER TO postgres;

--
-- Name: deuteriumenrichment; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.deuteriumenrichment (
    deuteriumid integer NOT NULL,
    electrolysisid integer,
    preenrichmentid integer,
    postenrichmentid integer,
    predeuterium double precision,
    predeuteriumunc double precision,
    postdeuterium double precision,
    postdeuteriumunc double precision,
    deuteriumrecovery double precision,
    cellconstant double precision,
    cellconstantunc double precision,
    enrichmentfactor double precision,
    enrichmentfactorunc double precision,
    remarks character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE trims.deuteriumenrichment OWNER TO postgres;

--
-- Name: electrolysis; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.electrolysis (
    electrolysisid integer NOT NULL,
    runid integer DEFAULT 0 NOT NULL,
    analysisid integer DEFAULT 0 NOT NULL,
    cellid smallint,
    distillationid integer,
    diluentpreenrichment double precision,
    massemptycell double precision,
    fullcellmassbefore double precision,
    fullcellmassafter double precision,
    emptybottlemass double precision,
    fullbottlemassbefore double precision,
    fullbottlemassafter double precision,
    cellmassafterneutralization double precision,
    coldtrapmassbefore double precision,
    coldtrapmassafter double precision,
    na2o2mass double precision,
    amperehour double precision,
    diluentmassafter double precision,
    pre2hsubsampletaken boolean DEFAULT false NOT NULL,
    post2hsubsampletaken boolean DEFAULT false NOT NULL,
    finaldistillationcomplete boolean DEFAULT false NOT NULL,
    enrichmentfactor double precision,
    enrichmentfactorunc double precision,
    enrichmentparam double precision,
    enrichmentparamunc double precision,
    epcomputemethod smallint,
    remarks character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50),
    isignored boolean DEFAULT false NOT NULL,
    w_init double precision,
    w_final double precision,
    w_init_unc double precision,
    w_final_unc double precision
);


ALTER TABLE trims.electrolysis OWNER TO postgres;

--
-- Name: electrolysisrun; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.electrolysisrun (
    runid integer DEFAULT nextval('public.trims_electrolysisrun_runid_seq'::regclass) NOT NULL,
    workflowid smallint,
    workflowjobid smallint,
    procedureid integer,
    elyssystemid integer,
    runstarttime timestamp without time zone,
    runendtime timestamp without time zone,
    enrichmentparam double precision,
    enrichmentparamunc double precision,
    remarks character varying(255),
    technicianid integer,
    technician2 integer,
    runstatus smallint,
    islocked boolean DEFAULT false NOT NULL,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE trims.electrolysisrun OWNER TO postgres;

--
-- Name: lscloadlist; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.lscloadlist (
    countid integer NOT NULL,
    runid integer DEFAULT 0 NOT NULL,
    analysisid integer DEFAULT 0 NOT NULL,
    electrolysisid integer,
    sampletype smallint DEFAULT 0 NOT NULL,
    sampleamount real NOT NULL,
    samplediluent real,
    positioninrun smallint DEFAULT 0 NOT NULL,
    traynumber smallint,
    positionintray smallint,
    counttime double precision,
    result double precision,
    resultunc double precision,
    status smallint,
    remarks character varying(255),
    isdecaycorrected boolean DEFAULT false NOT NULL,
    islocked boolean DEFAULT false NOT NULL,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    isignored boolean DEFAULT false NOT NULL,
    qc_decision smallint DEFAULT 0 NOT NULL,
    repeat_status character varying(10) DEFAULT 'PENDING'::character varying NOT NULL
);


ALTER TABLE trims.lscloadlist OWNER TO postgres;

--
-- Name: lscresult; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.lscresult (
    lscresultid integer NOT NULL,
    analysisid integer,
    runid integer,
    enrichmentfactor double precision,
    enrichmentfactorunc double precision,
    enrichmentfactormethod smallint,
    finalactivity double precision,
    finalactivityunc double precision,
    activityunit smallint,
    lldstatus smallint,
    rejectflag smallint DEFAULT 0 NOT NULL,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50)
);


ALTER TABLE trims.lscresult OWNER TO postgres;

--
-- Name: lscrun; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.lscrun (
    runid integer DEFAULT nextval('public.trims_lscrun_runid_seq'::regclass) NOT NULL,
    workflowid smallint,
    workflowjobid smallint,
    equipmentid smallint,
    procedureid integer,
    datapath character varying(255),
    headerstoimport character varying(255),
    storedheaders character varying(255),
    technicianid integer,
    technician2 integer,
    runstarttime timestamp without time zone,
    runendtime timestamp without time zone,
    minutescompleted integer,
    meanbackground double precision,
    meanbackgroundunc double precision,
    meanstandard double precision,
    meanstandardunc double precision,
    counterefficiency double precision,
    counterefficiencyunc double precision,
    calibrationfactor double precision,
    calibrationfactorunc double precision,
    lc double precision,
    lld double precision,
    islocked boolean DEFAULT false NOT NULL,
    outliermethod character varying(25),
    outliersigma double precision,
    runstatus smallint,
    remarks character varying(255),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE trims.lscrun OWNER TO postgres;

--
-- Name: lscrunmean; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.lscrunmean (
    countid integer DEFAULT nextval('public.trims_lscrunmean_countid_seq'::regclass) NOT NULL,
    valuekind smallint DEFAULT 0 NOT NULL,
    meanvalue double precision,
    meanvalueunc double precision,
    remarks character varying(100),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50)
);


ALTER TABLE trims.lscrunmean OWNER TO postgres;

--
-- Name: vwelectrolysiscellconstant; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.vwelectrolysiscellconstant AS
 SELECT ll.runid AS lscrunid,
    e.runid AS elysrunid,
    er.elyssystemid,
    er.runendtime AS dtmelysrunend,
    a.countid,
    e.cellid,
    e.analysisid,
    ll.sampleamount,
    ll.sampletype,
    ll.samplediluent,
    e.enrichmentfactor AS elysenrfactor,
    lr.enrichmentfactorunc AS elysenrfactorunc,
    a.meanvalue AS ef_3h,
    a.meanvalueunc AS ef_3hunc,
    lscrun.equipmentid,
    de.predeuterium,
    de.predeuteriumunc,
    de.postdeuterium,
    de.postdeuteriumunc,
    de.cellconstant,
    de.cellconstantunc,
    de.enrichmentfactor AS "2h_enrfactor",
    de.enrichmentfactorunc AS "2h_enrfactorunc",
    de.deuteriumrecovery,
    e.na2o2mass,
    e.massemptycell,
    e.fullcellmassbefore,
    e.diluentpreenrichment,
    e.fullcellmassafter,
    e.diluentmassafter,
    e.emptybottlemass,
    e.fullbottlemassbefore,
    e.fullbottlemassafter,
    e.cellmassafterneutralization,
    e.coldtrapmassbefore,
    e.coldtrapmassafter
   FROM ((((((trims.lscrun lscrun
     JOIN trims.lscloadlist ll ON ((lscrun.runid = ll.runid)))
     JOIN ( SELECT lscrunmean.countid,
            lscrunmean.meanvalue,
            lscrunmean.meanvalueunc
           FROM trims.lscrunmean
          WHERE (lscrunmean.valuekind = '-4'::integer)) a ON ((ll.countid = a.countid)))
     JOIN trims.lscresult lr ON (((ll.runid = lr.runid) AND (ll.analysisid = lr.analysisid))))
     JOIN trims.electrolysis e ON ((ll.analysisid = e.analysisid)))
     JOIN trims.electrolysisrun er ON ((er.runid = e.runid)))
     JOIN trims.deuteriumenrichment de ON ((e.electrolysisid = de.electrolysisid)))
  WHERE (ll.sampletype = 3);


ALTER VIEW public.vwelectrolysiscellconstant OWNER TO postgres;

--
-- Name: vwguiresult4home; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.vwguiresult4home AS
 SELECT s.mediaid,
    a.analysisid,
    s.prefix,
    s.sampleid,
    s.sname,
    s.submissionid,
    s.status AS samplestatus,
    NULLIF((COALESCE(sl1.description, sl.description))::text, ''::text) AS astatus,
    s.status,
    lr.finalactivity,
    lr.activityunit,
    lr.finalactivityunc,
    lr.rejectflag,
    lr.lldstatus,
    e.runid AS elysrunid,
    ll.runid AS lscrunid,
    lr.enrichmentfactor,
    lr.enrichmentfactorunc,
    de.predeuterium,
    de.postdeuterium,
    ll.countid
   FROM (((((((public.sample s
     JOIN public.statuslookup sl ON ((sl.status = s.status)))
     LEFT JOIN public.analysis a ON (((s.sampleid = a.sampleid) AND ((s.prefix)::text = (a.prefix)::text))))
     LEFT JOIN trims.lscresult lr ON ((a.analysisid = lr.analysisid)))
     LEFT JOIN trims.electrolysis e ON ((a.analysisid = e.analysisid)))
     LEFT JOIN trims.deuteriumenrichment de ON ((e.electrolysisid = de.electrolysisid)))
     LEFT JOIN trims.lscloadlist ll ON (((lr.analysisid = ll.analysisid) AND (lr.runid = ll.runid))))
     LEFT JOIN public.statuslookup sl1 ON ((sl1.status = a.status)))
  WHERE (s.submissionid <> 1);


ALTER VIEW public.vwguiresult4home OWNER TO postgres;

--
-- Name: vwlistofsubmissions; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.vwlistofsubmissions AS
 SELECT submission.submissionid AS id,
    submission.submissiondate AS submitted,
    submission.reporteddate AS reported,
    customer.lastname AS submitter,
    employee.lastname AS officer,
    submission.submissionname AS "submission name",
    ( SELECT count(sample.sampleid) AS count
           FROM public.sample
          WHERE (sample.submissionid = submission.submissionid)) AS no,
    submission.submissionsite AS location,
    submission.receivingno AS rcvno,
    priority.description AS priority,
    media.medianame AS media,
    statuslookup.description AS status
   FROM (((((public.submission
     JOIN public.media ON ((media.mediaid = submission.mediaid)))
     JOIN public.statuslookup ON ((statuslookup.status = submission.status)))
     JOIN public.priority ON ((priority.priorityid = submission.priorityid)))
     JOIN public.customer ON ((customer.customerid = submission.customerid)))
     LEFT JOIN public.employee ON ((submission.technicalofficer = employee.employeeid)));


ALTER VIEW public.vwlistofsubmissions OWNER TO postgres;

--
-- Name: vwlscloadlist; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.vwlscloadlist AS
 SELECT ll.runid,
    s.prefix,
    s.sampleid,
    s.sname,
    ll.analysisid,
    ll.positioninrun,
    ll.traynumber,
    ll.positionintray,
    ll.counttime,
    ll.sampleamount,
    ll.samplediluent,
    ll.sampletype,
    ll.islocked,
    ll.remarks,
    lr.enrichmentfactor,
    lr.enrichmentfactorunc,
    ll.result,
    ll.resultunc,
    a_mean.meanvalue,
    a_mean.meanvalueunc,
    lr.finalactivity,
    lr.finalactivityunc,
    lr.activityunit,
    lr.rejectflag AS isignored,
    NULL::text AS shortname,
    lr.lldstatus
   FROM (((((trims.lscloadlist ll
     JOIN public.analysis an ON ((ll.analysisid = an.analysisid)))
     JOIN public.sample s ON (((an.sampleid = s.sampleid) AND ((an.prefix)::text = (s.prefix)::text))))
     LEFT JOIN ( SELECT lscrunmean.countid,
            lscrunmean.valuekind,
            lscrunmean.meanvalue,
            lscrunmean.meanvalueunc,
            lscrunmean.remarks,
            lscrunmean.createdatestamp,
            lscrunmean.createuserstamp
           FROM trims.lscrunmean
          WHERE (lscrunmean.valuekind = 1)) a_mean ON ((ll.countid = a_mean.countid)))
     LEFT JOIN trims.lscresult lr ON (((ll.runid = lr.runid) AND (ll.analysisid = lr.analysisid))))
     LEFT JOIN trims.electrolysis e ON ((ll.analysisid = e.analysisid)))
  WHERE (ll.sampletype <> ALL (ARRAY[8, 9]));


ALTER VIEW public.vwlscloadlist OWNER TO postgres;

--
-- Name: vwlscmeancpm; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.vwlscmeancpm AS
 SELECT ll.runid,
    ll.positioninrun,
    e.runid AS erunid,
    e.cellid,
    a.analysisid,
    s.prefix,
    s.sampleid,
    s.sname,
    s.sampletype,
    max(
        CASE
            WHEN (rm.valuekind = 0) THEN rm.meanvalue
            ELSE NULL::double precision
        END) AS cpmmeanvalue,
    max(
        CASE
            WHEN (rm.valuekind = 0) THEN rm.meanvalueunc
            ELSE NULL::double precision
        END) AS cpmmeanvalueunc,
    max(
        CASE
            WHEN (rm.valuekind = 90) THEN rm.meanvalue
            ELSE NULL::double precision
        END) AS qipmeanvalue,
    max(
        CASE
            WHEN (rm.valuekind = 90) THEN rm.meanvalueunc
            ELSE NULL::double precision
        END) AS qipmeanvalueunc,
    max(
        CASE
            WHEN (rm.valuekind = 11) THEN rm.meanvalue
            ELSE NULL::double precision
        END) AS dpmmeanvalue,
    max(
        CASE
            WHEN (rm.valuekind = 11) THEN rm.meanvalueunc
            ELSE NULL::double precision
        END) AS dpmmeanvalueunc,
    e.enrichmentparam,
    e.enrichmentparamunc,
    ll.islocked,
    ll.remarks,
    ll.counttime AS totalcounttime
   FROM ((((trims.lscloadlist ll
     LEFT JOIN trims.electrolysis e ON ((ll.analysisid = e.analysisid)))
     JOIN public.analysis a ON ((ll.analysisid = a.analysisid)))
     JOIN public.sample s ON (((a.sampleid = s.sampleid) AND ((a.prefix)::text = (s.prefix)::text))))
     LEFT JOIN trims.lscrunmean rm ON (((rm.countid = ll.countid) AND (rm.valuekind = ANY (ARRAY[0, 11, 90])))))
  GROUP BY ll.runid, ll.positioninrun, a.analysisid, s.prefix, s.sampleid, s.sname, s.sampletype, ll.islocked, ll.remarks, e.runid, e.cellid, e.enrichmentparam, e.enrichmentparamunc, ll.counttime;


ALTER VIEW public.vwlscmeancpm OWNER TO postgres;

--
-- Name: workflow; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.workflow (
    workflowid smallint NOT NULL,
    workflowname character varying(255),
    abbreviation character varying(255),
    mediaid smallint,
    comments character varying(255),
    price double precision,
    reportingheadermemo text,
    reportingfootermemo text,
    isobsolete boolean DEFAULT false NOT NULL,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE public.workflow OWNER TO postgres;

--
-- Name: vwsampleanalysisstatus; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.vwsampleanalysisstatus AS
 SELECT s.prefix,
    s.sampleid,
    s.sname AS samplename,
    s.mediaid,
    concat_ws('-'::text, s.prefix, (s.sampleid)::text) AS ourlabid,
    a.analysisid,
    s.submissionid,
    COALESCE(a.status, s.status) AS status,
    (public.status_label(COALESCE(a.status, s.status), (med.module)::text))::character varying(255) AS description,
    (w.abbreviation)::text AS abbreviation,
    a.repeats,
    a.precursoranalysisid,
    COALESCE((a.phase)::integer,
        CASE
            WHEN (s.status = '-9'::integer) THEN '-1'::integer
            WHEN (s.status = 222) THEN 1
            WHEN (s.status = 1) THEN 0
            ELSE 0
        END) AS phase,
    pl.label AS phase_label,
    med.module AS media_module
   FROM (((((public.sample s
     JOIN public.submission sub ON ((s.submissionid = sub.submissionid)))
     LEFT JOIN public.analysis a ON (((s.sampleid = a.sampleid) AND ((s.prefix)::text = (a.prefix)::text))))
     LEFT JOIN public.media med ON ((med.mediaid = s.mediaid)))
     LEFT JOIN public.phaselookup pl ON ((pl.phase = COALESCE((a.phase)::integer,
        CASE
            WHEN (s.status = '-9'::integer) THEN '-1'::integer
            WHEN (s.status = 222) THEN 1
            WHEN (s.status = 1) THEN 0
            ELSE 0
        END))))
     LEFT JOIN public.workflow w ON ((a.workflowid = w.workflowid)))
  WHERE (sub.submissiontype <> 1);


ALTER VIEW public.vwsampleanalysisstatus OWNER TO postgres;

--
-- Name: sianalysisloadlist; Type: TABLE; Schema: siam; Owner: postgres
--

CREATE TABLE siam.sianalysisloadlist (
    sianalysisid integer NOT NULL,
    sianalysisrunid integer DEFAULT 0 NOT NULL,
    analysisid integer DEFAULT 0 NOT NULL,
    repeat smallint DEFAULT 1 NOT NULL,
    blockno smallint,
    positioninrun smallint DEFAULT 0 NOT NULL,
    traynumber smallint,
    positionintray smallint,
    injections smallint,
    status smallint,
    aliquotsize double precision,
    precursorid integer,
    instrumentanalysisid character varying(255),
    isignored boolean,
    remarks character varying(100),
    qc_decision smallint DEFAULT 0 NOT NULL,
    repeat_status character varying(10) DEFAULT 'PENDING'::character varying NOT NULL
);


ALTER TABLE siam.sianalysisloadlist OWNER TO postgres;

--
-- Name: vwsirepeatanalysis; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.vwsirepeatanalysis AS
 SELECT analysis.analysisid,
    analysis.repeats,
    (analysis.repeats - count(sianalysisloadlist.analysisid)) AS pendingrepeats,
    max(sianalysisloadlist.repeat) AS repeat,
    max(sianalysisloadlist.status) AS status
   FROM (siam.sianalysisloadlist
     JOIN public.analysis ON ((sianalysisloadlist.analysisid = analysis.analysisid)))
  GROUP BY analysis.analysisid, analysis.repeats
 HAVING (count(analysis.analysisid) < analysis.repeats);


ALTER VIEW public.vwsirepeatanalysis OWNER TO postgres;

--
-- Name: vwstationlateststatus; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.vwstationlateststatus AS
 SELECT t1.lngstationautoid,
    t2.intstatusid AS curstatus
   FROM (( SELECT tblmatchstationstatus.lngstationautoid,
            max(tblmatchstationstatus.lngstationstatusid) AS lateststatuschange
           FROM public.tblmatchstationstatus
          GROUP BY tblmatchstationstatus.lngstationautoid) t1
     JOIN public.tblmatchstationstatus t2 ON ((t1.lateststatuschange = t2.lngstationstatusid)));


ALTER VIEW public.vwstationlateststatus OWNER TO postgres;

--
-- Name: workflow_workflowid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.workflow ALTER COLUMN workflowid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.workflow_workflowid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: workflowjob; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.workflowjob (
    workflowjobid smallint DEFAULT nextval('public.public_workflowjob_workflowjobid_seq'::regclass) NOT NULL,
    workflowid smallint,
    jobname character varying(255),
    procedureid integer,
    runsequence smallint,
    isprerequisite boolean,
    isobsolete boolean,
    employeeid integer,
    equipmentid integer,
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    isreportingjob boolean DEFAULT false NOT NULL
);


ALTER TABLE public.workflowjob OWNER TO postgres;

--
-- Name: sianalysiscorrection; Type: TABLE; Schema: siam; Owner: postgres
--

CREATE TABLE siam.sianalysiscorrection (
    sianalysisrunid integer DEFAULT nextval('public.siam_sianalysiscorrection_sianalysisrunid_seq'::regclass) NOT NULL,
    correctiontype smallint DEFAULT 0 NOT NULL,
    correctionsubtype smallint,
    ourlabid character varying(255),
    isapplied boolean,
    remarks character varying(255)
);


ALTER TABLE siam.sianalysiscorrection OWNER TO postgres;

--
-- Name: sianalysiscorrectionfit; Type: TABLE; Schema: siam; Owner: postgres
--

CREATE TABLE siam.sianalysiscorrectionfit (
    id integer NOT NULL,
    sianalysisrunid integer DEFAULT 0 NOT NULL,
    measurableid integer DEFAULT 0 NOT NULL,
    prefix character varying(1) DEFAULT ''::character varying NOT NULL,
    sampleid integer DEFAULT 0 NOT NULL,
    driftslope double precision,
    driftslopeunc double precision,
    linslope double precision,
    linslopeunc double precision,
    linintercept double precision,
    lininterceptunc double precision,
    driftintercept double precision,
    driftinterceptunc double precision,
    busedforlin boolean,
    busedformem boolean,
    busedfordrift boolean,
    busedforzscore boolean,
    busedfornorm boolean,
    fcertifiedvalue double precision,
    fcertifiedvalueunc double precision
);


ALTER TABLE siam.sianalysiscorrectionfit OWNER TO postgres;

--
-- Name: sianalysiscorrectionfit_id_seq; Type: SEQUENCE; Schema: siam; Owner: postgres
--

ALTER TABLE siam.sianalysiscorrectionfit ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME siam.sianalysiscorrectionfit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sianalysiscorrectionfitinj; Type: TABLE; Schema: siam; Owner: postgres
--

CREATE TABLE siam.sianalysiscorrectionfitinj (
    sianalysisrunid integer DEFAULT nextval('public.siam_sianalysiscorrectionfitinj_sianalysisrunid_seq'::regclass) NOT NULL,
    correctiontype smallint DEFAULT 0 NOT NULL,
    injectionno smallint DEFAULT 0 NOT NULL,
    measurableid smallint DEFAULT 0 NOT NULL,
    dblvalue double precision,
    dblvalueunc double precision,
    remarks character varying(255)
);


ALTER TABLE siam.sianalysiscorrectionfitinj OWNER TO postgres;

--
-- Name: sianalysisinjectiondata; Type: TABLE; Schema: siam; Owner: postgres
--

CREATE TABLE siam.sianalysisinjectiondata (
    sianalysisid integer DEFAULT nextval('public.siam_sianalysisinjectiondata_sianalysisid_seq'::regclass) NOT NULL,
    injectionno smallint DEFAULT 0 NOT NULL,
    analysistime timestamp without time zone,
    signal double precision,
    instrumentflags character varying(255),
    instrumenttemperature double precision,
    instrumentpressure double precision,
    isignored boolean,
    remarks character varying(100)
);


ALTER TABLE siam.sianalysisinjectiondata OWNER TO postgres;

--
-- Name: sianalysisinterimdata; Type: TABLE; Schema: siam; Owner: postgres
--

CREATE TABLE siam.sianalysisinterimdata (
    sianalysisdataid integer DEFAULT nextval('public.siam_sianalysisinterimdata_sianalysisdataid_seq'::regclass) NOT NULL,
    valueid smallint DEFAULT 0 NOT NULL,
    fvalue double precision NOT NULL,
    fvalueunc double precision
);


ALTER TABLE siam.sianalysisinterimdata OWNER TO postgres;

--
-- Name: sianalysisloadlist_sianalysisid_seq; Type: SEQUENCE; Schema: siam; Owner: postgres
--

ALTER TABLE siam.sianalysisloadlist ALTER COLUMN sianalysisid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME siam.sianalysisloadlist_sianalysisid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sianalysisrawdata; Type: TABLE; Schema: siam; Owner: postgres
--

CREATE TABLE siam.sianalysisrawdata (
    sianalysisdataid integer NOT NULL,
    sianalysisid integer DEFAULT 0 NOT NULL,
    injectionno smallint DEFAULT 0 NOT NULL,
    measurableid smallint DEFAULT 0 NOT NULL,
    valueid smallint,
    fvalue double precision NOT NULL,
    fvalueunc double precision
);


ALTER TABLE siam.sianalysisrawdata OWNER TO postgres;

--
-- Name: sianalysisrawdata_sianalysisdataid_seq; Type: SEQUENCE; Schema: siam; Owner: postgres
--

ALTER TABLE siam.sianalysisrawdata ALTER COLUMN sianalysisdataid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME siam.sianalysisrawdata_sianalysisdataid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sianalysisresult; Type: TABLE; Schema: siam; Owner: postgres
--

CREATE TABLE siam.sianalysisresult (
    sianalysisid integer DEFAULT nextval('public.siam_sianalysisresult_sianalysisid_seq'::regclass) NOT NULL,
    measurableid smallint DEFAULT 0 NOT NULL,
    starttime timestamp without time zone,
    fsignal double precision,
    ffirstaverage double precision,
    ffirstuncertainty double precision,
    fvalue double precision NOT NULL,
    fvalueunc double precision NOT NULL,
    isignored boolean DEFAULT false NOT NULL,
    createdatestamp timestamp without time zone DEFAULT now() NOT NULL
);


ALTER TABLE siam.sianalysisresult OWNER TO postgres;

--
-- Name: sianalysisrun; Type: TABLE; Schema: siam; Owner: postgres
--

CREATE TABLE siam.sianalysisrun (
    sianalysisrunid integer DEFAULT nextval('public.siam_sianalysisrun_sianalysisrunid_seq'::regclass) NOT NULL,
    equipmentid integer,
    workflowid smallint,
    workflowjobid smallint,
    procedureid integer,
    measurables character varying(255),
    datapath character varying(255),
    headerstoimport character varying(255),
    storedheaders character varying(255),
    technicianid integer,
    technician2 integer,
    runstarttime timestamp without time zone,
    runendtime timestamp without time zone,
    islocked boolean DEFAULT false NOT NULL,
    outliermethod character varying(25),
    runstatus smallint,
    remarks character varying(100),
    createdatestamp timestamp without time zone,
    createuserstamp character varying(50),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE siam.sianalysisrun OWNER TO postgres;

--
-- Name: siinlets; Type: TABLE; Schema: siam; Owner: postgres
--

CREATE TABLE siam.siinlets (
    siid integer DEFAULT nextval('public.siam_siinlets_siid_seq'::regclass) NOT NULL,
    iinletno integer DEFAULT 0 NOT NULL,
    itime integer,
    iamplitude integer DEFAULT 0 NOT NULL,
    fsignal double precision,
    fratio double precision,
    blocked boolean
);


ALTER TABLE siam.siinlets OWNER TO postgres;

--
-- Name: simeasurement; Type: TABLE; Schema: siam; Owner: postgres
--

CREATE TABLE siam.simeasurement (
    siid integer DEFAULT nextval('public.siam_simeasurement_siid_seq'::regclass) NOT NULL,
    sianalysisid integer DEFAULT 0 NOT NULL,
    measurableid integer DEFAULT 0 NOT NULL,
    starttime timestamp without time zone,
    fsignal double precision,
    ffirstaverage double precision,
    ffirstuncertainty double precision,
    fvalue double precision,
    funcertainty double precision,
    isignored boolean,
    remarks character varying(100)
);


ALTER TABLE siam.simeasurement OWNER TO postgres;

--
-- Name: chemenrrun; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.chemenrrun (
    runid integer NOT NULL,
    measurableid integer NOT NULL,
    enrichmentmethod smallint NOT NULL,
    rundate timestamp with time zone,
    isfinished boolean DEFAULT false NOT NULL,
    technicianid integer,
    remarks character varying(255),
    createdatestamp timestamp with time zone DEFAULT now() NOT NULL,
    createuserstamp character varying(50) DEFAULT CURRENT_USER NOT NULL,
    modifdatestamp timestamp with time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE trims.chemenrrun OWNER TO postgres;

--
-- Name: TABLE chemenrrun; Type: COMMENT; Schema: trims; Owner: postgres
--

COMMENT ON TABLE trims.chemenrrun IS 'Run-level header for chemical pre-concentration enrichment batches (35S, 14C, …)';


--
-- Name: COLUMN chemenrrun.enrichmentmethod; Type: COMMENT; Schema: trims; Owner: postgres
--

COMMENT ON COLUMN trims.chemenrrun.enrichmentmethod IS '5=Gravimetric, 6=SpikeRecovery, 7=Volumetric';


--
-- Name: COLUMN chemenrrun.isfinished; Type: COMMENT; Schema: trims; Owner: postgres
--

COMMENT ON COLUMN trims.chemenrrun.isfinished IS 'TRUE once the run is complete and Analysis.Status has been propagated';


--
-- Name: chemenrrun_runid_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

CREATE SEQUENCE trims.chemenrrun_runid_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE trims.chemenrrun_runid_seq OWNER TO postgres;

--
-- Name: chemenrrun_runid_seq; Type: SEQUENCE OWNED BY; Schema: trims; Owner: postgres
--

ALTER SEQUENCE trims.chemenrrun_runid_seq OWNED BY trims.chemenrrun.runid;


--
-- Name: chemicalenrichment; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.chemicalenrichment (
    chemenrid integer NOT NULL,
    runid integer NOT NULL,
    analysisid integer NOT NULL,
    measurableid integer NOT NULL,
    initialvolumeml double precision,
    initialvoluncml double precision,
    initialmassg double precision,
    initialmassuncg double precision,
    finalvolumeml double precision,
    finalvoluncml double precision,
    finalmassg double precision,
    finalmassuncg double precision,
    carriermassg double precision,
    carriermassuncg double precision,
    spikeaddeddpm double precision,
    spikeaddeddpmunc double precision,
    spikemeasureddpm double precision,
    spikemeasureddpmunc double precision,
    recoveryfraction double precision,
    recoveryfracunc double precision,
    enrichmentfactor double precision,
    enrichmentfactorunc double precision,
    enrichmentmethod smallint,
    isignored boolean DEFAULT false NOT NULL,
    enricheddate timestamp with time zone,
    remarks character varying(255),
    createdatestamp timestamp with time zone DEFAULT now() NOT NULL,
    createuserstamp character varying(50) DEFAULT CURRENT_USER NOT NULL,
    modifdatestamp timestamp with time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE trims.chemicalenrichment OWNER TO postgres;

--
-- Name: TABLE chemicalenrichment; Type: COMMENT; Schema: trims; Owner: postgres
--

COMMENT ON TABLE trims.chemicalenrichment IS 'Per-sample chemical enrichment measurements (volumes, masses, spike recovery, computed EF)';


--
-- Name: COLUMN chemicalenrichment.enrichmentfactor; Type: COMMENT; Schema: trims; Owner: postgres
--

COMMENT ON COLUMN trims.chemicalenrichment.enrichmentfactor IS 'Computed: (InitVol/FinalVol) * RecoveryFraction';


--
-- Name: COLUMN chemicalenrichment.enrichmentmethod; Type: COMMENT; Schema: trims; Owner: postgres
--

COMMENT ON COLUMN trims.chemicalenrichment.enrichmentmethod IS '5=Gravimetric, 6=SpikeRecovery, 7=Volumetric';


--
-- Name: COLUMN chemicalenrichment.isignored; Type: COMMENT; Schema: trims; Owner: postgres
--

COMMENT ON COLUMN trims.chemicalenrichment.isignored IS 'TRUE = excluded from final activity computation';


--
-- Name: chemicalenrichment_chemenrid_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

CREATE SEQUENCE trims.chemicalenrichment_chemenrid_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE trims.chemicalenrichment_chemenrid_seq OWNER TO postgres;

--
-- Name: chemicalenrichment_chemenrid_seq; Type: SEQUENCE OWNED BY; Schema: trims; Owner: postgres
--

ALTER SEQUENCE trims.chemicalenrichment_chemenrid_seq OWNED BY trims.chemicalenrichment.chemenrid;


--
-- Name: deuteriumenrichment_deuteriumid_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

ALTER TABLE trims.deuteriumenrichment ALTER COLUMN deuteriumid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME trims.deuteriumenrichment_deuteriumid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: electrolysis_electrolysisid_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

ALTER TABLE trims.electrolysis ALTER COLUMN electrolysisid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME trims.electrolysis_electrolysisid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: guitblimportmapping; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.guitblimportmapping (
    mappingid integer NOT NULL,
    formatid integer DEFAULT 0 NOT NULL,
    isotopeid integer DEFAULT 0 NOT NULL,
    targetfield character varying(50) DEFAULT ''::character varying NOT NULL,
    sourceheader character varying(100) DEFAULT ''::character varying NOT NULL,
    updatedate timestamp without time zone,
    updateuser character varying(50)
);


ALTER TABLE trims.guitblimportmapping OWNER TO postgres;

--
-- Name: guitblimportmapping_mappingid_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

ALTER TABLE trims.guitblimportmapping ALTER COLUMN mappingid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME trims.guitblimportmapping_mappingid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: lscloadlist_countid_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

ALTER TABLE trims.lscloadlist ALTER COLUMN countid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME trims.lscloadlist_countid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: lscprocedureprotocol; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.lscprocedureprotocol (
    procedureprotocolid integer NOT NULL,
    procedureid integer DEFAULT 0 NOT NULL,
    protocolid integer DEFAULT 0 NOT NULL,
    effectivedate date DEFAULT now() NOT NULL,
    expirydate date,
    isactive boolean,
    setby character varying(50),
    setdate timestamp without time zone
);


ALTER TABLE trims.lscprocedureprotocol OWNER TO postgres;

--
-- Name: lscprocedureprotocol_procedureprotocolid_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

ALTER TABLE trims.lscprocedureprotocol ALTER COLUMN procedureprotocolid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME trims.lscprocedureprotocol_procedureprotocolid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: lscprotocol; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.lscprotocol (
    protocolid integer NOT NULL,
    protocolname character varying(100) DEFAULT ''::character varying NOT NULL,
    isotopeid integer DEFAULT 0 NOT NULL,
    fileformatid integer DEFAULT 0 NOT NULL,
    description character varying(500),
    isdefault boolean,
    isactive boolean,
    createdby character varying(50),
    createddate timestamp without time zone,
    modifiedby character varying(50),
    modifieddate timestamp without time zone
);


ALTER TABLE trims.lscprotocol OWNER TO postgres;

--
-- Name: lscprotocol_protocolid_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

ALTER TABLE trims.lscprotocol ALTER COLUMN protocolid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME trims.lscprotocol_protocolid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: lscprotocolmapping; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.lscprotocolmapping (
    mappingid integer NOT NULL,
    protocolid integer DEFAULT 0 NOT NULL,
    targetfield character varying(50) DEFAULT ''::character varying NOT NULL,
    sourceheader character varying(200),
    isnet boolean,
    requiresbackground boolean,
    uncertaintycolumn character varying(200),
    transformformula character varying(500),
    displayorder integer,
    notes character varying(500)
);


ALTER TABLE trims.lscprotocolmapping OWNER TO postgres;

--
-- Name: lscprotocolmapping_mappingid_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

ALTER TABLE trims.lscprotocolmapping ALTER COLUMN mappingid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME trims.lscprotocolmapping_mappingid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: lscprotocolsettings; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.lscprotocolsettings (
    settingid integer NOT NULL,
    protocolid integer DEFAULT 0 NOT NULL,
    signalmetric character varying(10),
    efficiencysource character varying(50),
    outliermethod character varying(50),
    outlierthreshold double precision,
    outlieriterations integer,
    outlierapplyto character varying(10),
    activityunit integer,
    enrichmentfactormethod integer,
    backgroundmode character varying(50),
    backgroundvalue double precision,
    mincounttime double precision,
    maxrsd double precision,
    requireqipcheck boolean
);


ALTER TABLE trims.lscprotocolsettings OWNER TO postgres;

--
-- Name: lscprotocolsettings_settingid_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

ALTER TABLE trims.lscprotocolsettings ALTER COLUMN settingid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME trims.lscprotocolsettings_settingid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: lscresult_lscresultid_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

ALTER TABLE trims.lscresult ALTER COLUMN lscresultid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME trims.lscresult_lscresultid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: lscrunprotocol; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.lscrunprotocol (
    runprotocolid integer NOT NULL,
    runid integer DEFAULT 0 NOT NULL,
    protocolid integer,
    protocolsnapshot text DEFAULT ''::text NOT NULL,
    wasmodified boolean,
    appliedby character varying(50),
    applieddate timestamp without time zone
);


ALTER TABLE trims.lscrunprotocol OWNER TO postgres;

--
-- Name: lscrunprotocol_runprotocolid_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

ALTER TABLE trims.lscrunprotocol ALTER COLUMN runprotocolid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME trims.lscrunprotocol_runprotocolid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: lscrunraw; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.lscrunraw (
    countrawid integer NOT NULL,
    countid integer DEFAULT 0 NOT NULL,
    repeat smallint,
    cycleno smallint DEFAULT 0 NOT NULL,
    valuekind smallint DEFAULT 0 NOT NULL,
    cyclevalue double precision,
    isrejected boolean DEFAULT false NOT NULL
);


ALTER TABLE trims.lscrunraw OWNER TO postgres;

--
-- Name: lscrunraw_countrawid_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

ALTER TABLE trims.lscrunraw ALTER COLUMN countrawid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME trims.lscrunraw_countrawid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: lsctrayconfig; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.lsctrayconfig (
    equipmentid integer NOT NULL,
    traycount smallint DEFAULT 1 NOT NULL,
    vialcapacity smallint NOT NULL
);


ALTER TABLE trims.lsctrayconfig OWNER TO postgres;

--
-- Name: TABLE lsctrayconfig; Type: COMMENT; Schema: trims; Owner: postgres
--

COMMENT ON TABLE trims.lsctrayconfig IS 'Tray layout for each LSC instrument.';


--
-- Name: COLUMN lsctrayconfig.traycount; Type: COMMENT; Schema: trims; Owner: postgres
--

COMMENT ON COLUMN trims.lsctrayconfig.traycount IS 'Number of physical trays the instrument holds.';


--
-- Name: COLUMN lsctrayconfig.vialcapacity; Type: COMMENT; Schema: trims; Owner: postgres
--

COMMENT ON COLUMN trims.lsctrayconfig.vialcapacity IS 'Maximum vials per tray.';


--
-- Name: primarydistillation; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.primarydistillation (
    id integer NOT NULL,
    analysisid integer DEFAULT 0 NOT NULL,
    runid integer DEFAULT 0 NOT NULL,
    flaskid smallint DEFAULT 0 NOT NULL,
    ecbeforer double precision,
    ecafter double precision,
    repeat smallint DEFAULT 1,
    status smallint,
    remarks character varying(255)
);


ALTER TABLE trims.primarydistillation OWNER TO postgres;

--
-- Name: primarydistillation_id_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

ALTER TABLE trims.primarydistillation ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME trims.primarydistillation_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: primarydistillationbatch; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.primarydistillationbatch (
    runid integer DEFAULT nextval('public.trims_primarydistillationbatch_runid_seq'::regclass) NOT NULL,
    workflowid smallint,
    workflowjobid smallint,
    procedureid integer,
    equipmentid integer,
    startdate timestamp without time zone,
    enddate timestamp without time zone,
    technicianid integer,
    technician2 integer,
    islocked boolean DEFAULT false NOT NULL,
    createuserstamp character varying(50),
    createdatestamp timestamp without time zone,
    remarks character varying(255)
);


ALTER TABLE trims.primarydistillationbatch OWNER TO postgres;

--
-- Name: primarydistillationdata; Type: TABLE; Schema: trims; Owner: postgres
--

CREATE TABLE trims.primarydistillationdata (
    distillationdataid integer NOT NULL,
    id integer DEFAULT 0 NOT NULL,
    flaskid smallint DEFAULT 0 NOT NULL,
    analysisid integer DEFAULT 0 NOT NULL,
    subanalysisid integer,
    ecafter double precision,
    status smallint,
    repeat smallint,
    remarks character varying(255),
    modifdatestamp timestamp without time zone,
    modifuserstamp character varying(50)
);


ALTER TABLE trims.primarydistillationdata OWNER TO postgres;

--
-- Name: primarydistillationdata_distillationdataid_seq; Type: SEQUENCE; Schema: trims; Owner: postgres
--

ALTER TABLE trims.primarydistillationdata ALTER COLUMN distillationdataid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME trims.primarydistillationdata_distillationdataid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: logged_actions action_id; Type: DEFAULT; Schema: audit; Owner: postgres
--

ALTER TABLE ONLY audit.logged_actions ALTER COLUMN action_id SET DEFAULT nextval('audit.logged_actions_action_id_seq'::regclass);


--
-- Name: ng3hesequenceresults resultid; Type: DEFAULT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng3hesequenceresults ALTER COLUMN resultid SET DEFAULT nextval('ngam.ng3hesequenceresults_resultid_seq'::regclass);


--
-- Name: ng_cf_template template_id; Type: DEFAULT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_cf_template ALTER COLUMN template_id SET DEFAULT nextval('ngam.ng_cf_template_template_id_seq'::regclass);


--
-- Name: ng_eqw_run eqw_run_id; Type: DEFAULT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_eqw_run ALTER COLUMN eqw_run_id SET DEFAULT nextval('ngam.ng_eqw_run_eqw_run_id_seq'::regclass);


--
-- Name: ng_pipette id; Type: DEFAULT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_pipette ALTER COLUMN id SET DEFAULT nextval('ngam.ng_pipette_id_seq'::regclass);


--
-- Name: ng_reference_vessel id; Type: DEFAULT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_reference_vessel ALTER COLUMN id SET DEFAULT nextval('ngam.ng_reference_vessel_id_seq'::regclass);


--
-- Name: ngbgproxyfactor id; Type: DEFAULT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngbgproxyfactor ALTER COLUMN id SET DEFAULT nextval('ngam.ngbgproxyfactor_id_seq'::regclass);


--
-- Name: ngdilutionfactor dilutionid; Type: DEFAULT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngdilutionfactor ALTER COLUMN dilutionid SET DEFAULT nextval('ngam.ngdilutionfactor_dilutionid_seq'::regclass);


--
-- Name: ngextractiondata extractionid; Type: DEFAULT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngextractiondata ALTER COLUMN extractionid SET DEFAULT nextval('ngam.ngextractiondata_extractionid_seq'::regclass);


--
-- Name: ngextractionlineefficiency efficiencyid; Type: DEFAULT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngextractionlineefficiency ALTER COLUMN efficiencyid SET DEFAULT nextval('ngam.ngextractionlineefficiency_efficiencyid_seq'::regclass);


--
-- Name: nglinearitysnapshots snapshot_id; Type: DEFAULT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.nglinearitysnapshots ALTER COLUMN snapshot_id SET DEFAULT nextval('ngam.nglinearitysnapshots_snapshot_id_seq'::regclass);


--
-- Name: app_log log_id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.app_log ALTER COLUMN log_id SET DEFAULT nextval('public.app_log_log_id_seq'::regclass);


--
-- Name: cims_item item_id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cims_item ALTER COLUMN item_id SET DEFAULT nextval('public.cims_item_item_id_seq'::regclass);


--
-- Name: cims_lot lot_id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cims_lot ALTER COLUMN lot_id SET DEFAULT nextval('public.cims_lot_lot_id_seq'::regclass);


--
-- Name: cims_supplier supplier_id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cims_supplier ALTER COLUMN supplier_id SET DEFAULT nextval('public.cims_supplier_supplier_id_seq'::regclass);


--
-- Name: cims_usage usage_id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cims_usage ALTER COLUMN usage_id SET DEFAULT nextval('public.cims_usage_usage_id_seq'::regclass);


--
-- Name: matrix matrixid; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.matrix ALTER COLUMN matrixid SET DEFAULT nextval('public.matrix_matrixid_seq'::regclass);


--
-- Name: processing_jobs job_id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.processing_jobs ALTER COLUMN job_id SET DEFAULT nextval('public.processing_jobs_job_id_seq'::regclass);


--
-- Name: sample_queue queue_id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample_queue ALTER COLUMN queue_id SET DEFAULT nextval('public.sample_queue_queue_id_seq'::regclass);


--
-- Name: templatemetadata templateid; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.templatemetadata ALTER COLUMN templateid SET DEFAULT nextval('public.templatemetadata_templateid_seq'::regclass);


--
-- Name: chemenrrun runid; Type: DEFAULT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.chemenrrun ALTER COLUMN runid SET DEFAULT nextval('trims.chemenrrun_runid_seq'::regclass);


--
-- Name: chemicalenrichment chemenrid; Type: DEFAULT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.chemicalenrichment ALTER COLUMN chemenrid SET DEFAULT nextval('trims.chemicalenrichment_chemenrid_seq'::regclass);


--
-- Name: amsmeasurement amsmeasurement_pkey; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amsmeasurement
    ADD CONSTRAINT amsmeasurement_pkey PRIMARY KEY (amsmeasurementid);


--
-- Name: amsresult amsresult_pkey; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amsresult
    ADD CONSTRAINT amsresult_pkey PRIMARY KEY (amsresultid);


--
-- Name: amsrun amsrun_pkey; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amsrun
    ADD CONSTRAINT amsrun_pkey PRIMARY KEY (amsrunid);


--
-- Name: amstarget amstarget_pkey; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amstarget
    ADD CONSTRAINT amstarget_pkey PRIMARY KEY (amstargetid);


--
-- Name: amswheel amswheel_pkey; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amswheel
    ADD CONSTRAINT amswheel_pkey PRIMARY KEY (amswheelid);


--
-- Name: graphrun graphrun_pkey; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.graphrun
    ADD CONSTRAINT graphrun_pkey PRIMARY KEY (graphrunid);


--
-- Name: graphsample graphsample_pkey; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.graphsample
    ADD CONSTRAINT graphsample_pkey PRIMARY KEY (graphsampleid);


--
-- Name: amsmeasurement uq_amsmeas_target_cycle; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amsmeasurement
    ADD CONSTRAINT uq_amsmeas_target_cycle UNIQUE (amstargetid, cyclenumber);


--
-- Name: amsresult uq_amsresult_target; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amsresult
    ADD CONSTRAINT uq_amsresult_target UNIQUE (amstargetid);


--
-- Name: amsrun uq_amsrun_runcode; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amsrun
    ADD CONSTRAINT uq_amsrun_runcode UNIQUE (runcode);


--
-- Name: amstarget uq_amstarget_wheel_pos; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amstarget
    ADD CONSTRAINT uq_amstarget_wheel_pos UNIQUE (amswheelid, wheelposition);


--
-- Name: amswheel uq_amswheel_run_num; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amswheel
    ADD CONSTRAINT uq_amswheel_run_num UNIQUE (amsrunid, wheelnumber);


--
-- Name: graphrun uq_graphrun_batchcode; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.graphrun
    ADD CONSTRAINT uq_graphrun_batchcode UNIQUE (batchcode);


--
-- Name: graphsample uq_graphsample_run_analysis; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.graphsample
    ADD CONSTRAINT uq_graphsample_run_analysis UNIQUE (graphrunid, analysisid);


--
-- Name: wheeltemplate uq_wheeltemplate_name; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.wheeltemplate
    ADD CONSTRAINT uq_wheeltemplate_name UNIQUE (templatename);


--
-- Name: wheeltemplate wheeltemplate_pkey; Type: CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.wheeltemplate
    ADD CONSTRAINT wheeltemplate_pkey PRIMARY KEY (templateid);


--
-- Name: logged_actions logged_actions_pkey; Type: CONSTRAINT; Schema: audit; Owner: postgres
--

ALTER TABLE ONLY audit.logged_actions
    ADD CONSTRAINT logged_actions_pkey PRIMARY KEY (action_id);


--
-- Name: batch chemistrybatch_pkey; Type: CONSTRAINT; Schema: chem; Owner: postgres
--

ALTER TABLE ONLY chem.batch
    ADD CONSTRAINT chemistrybatch_pkey PRIMARY KEY (runid);


--
-- Name: data chemistrydata_pkey; Type: CONSTRAINT; Schema: chem; Owner: postgres
--

ALTER TABLE ONLY chem.data
    ADD CONSTRAINT chemistrydata_pkey PRIMARY KEY (chemanalysisid, measurableid, repeat);


--
-- Name: loadlist chemistryloadlist_pkey; Type: CONSTRAINT; Schema: chem; Owner: postgres
--

ALTER TABLE ONLY chem.loadlist
    ADD CONSTRAINT chemistryloadlist_pkey PRIMARY KEY (chemanalysisid);


--
-- Name: chemproc chemistryprocedure_pkey; Type: CONSTRAINT; Schema: chem; Owner: postgres
--

ALTER TABLE ONLY chem.chemproc
    ADD CONSTRAINT chemistryprocedure_pkey PRIMARY KEY (procedureid);


--
-- Name: msrun msrun_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.msrun
    ADD CONSTRAINT msrun_pkey PRIMARY KEY (runid);


--
-- Name: ng3heingrowthdata ng3heingrowthdata_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng3heingrowthdata
    ADD CONSTRAINT ng3heingrowthdata_pkey PRIMARY KEY (ingrowthid);


--
-- Name: ng3heingrowthrun ng3heingrowthrun_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng3heingrowthrun
    ADD CONSTRAINT ng3heingrowthrun_pkey PRIMARY KEY (runid);


--
-- Name: ng3hesequenceloadlist ng3hesequenceloadlist_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng3hesequenceloadlist
    ADD CONSTRAINT ng3hesequenceloadlist_pkey PRIMARY KEY (headerid);


--
-- Name: ng3hesequenceraw ng3hesequenceraw_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng3hesequenceraw
    ADD CONSTRAINT ng3hesequenceraw_pkey PRIMARY KEY (ng3hesequencerawid);


--
-- Name: ng3hesequenceresults ng3hesequenceresults_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng3hesequenceresults
    ADD CONSTRAINT ng3hesequenceresults_pkey PRIMARY KEY (resultid);


--
-- Name: ng3hesequenceresults ng3hesequenceresults_runid_positioninrun_key; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng3hesequenceresults
    ADD CONSTRAINT ng3hesequenceresults_runid_positioninrun_key UNIQUE (runid, positioninrun);


--
-- Name: ng_cf_template ng_cf_template_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_cf_template
    ADD CONSTRAINT ng_cf_template_pkey PRIMARY KEY (template_id);


--
-- Name: ng_cf_template_run ng_cf_template_run_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_cf_template_run
    ADD CONSTRAINT ng_cf_template_run_pkey PRIMARY KEY (template_id, eqw_run_id);


--
-- Name: ng_eqw_run ng_eqw_run_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_eqw_run
    ADD CONSTRAINT ng_eqw_run_pkey PRIMARY KEY (eqw_run_id);


--
-- Name: ng_pipette ng_pipette_pipette_name_key; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_pipette
    ADD CONSTRAINT ng_pipette_pipette_name_key UNIQUE (pipette_name);


--
-- Name: ng_pipette ng_pipette_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_pipette
    ADD CONSTRAINT ng_pipette_pkey PRIMARY KEY (id);


--
-- Name: ng_reference_vessel ng_reference_vessel_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_reference_vessel
    ADD CONSTRAINT ng_reference_vessel_pkey PRIMARY KEY (id);


--
-- Name: ng_reference_vessel ng_reference_vessel_vessel_name_key; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_reference_vessel
    ADD CONSTRAINT ng_reference_vessel_vessel_name_key UNIQUE (vessel_name);


--
-- Name: ngbgproxyfactor ngbgproxyfactor_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngbgproxyfactor
    ADD CONSTRAINT ngbgproxyfactor_pkey PRIMARY KEY (id);


--
-- Name: ngblock ngblock_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngblock
    ADD CONSTRAINT ngblock_pkey PRIMARY KEY (iblockid);


--
-- Name: ngblockevaluation ngblockevaluation_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngblockevaluation
    ADD CONSTRAINT ngblockevaluation_pkey PRIMARY KEY (iblockid);


--
-- Name: ngblockfit ngblockfit_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngblockfit
    ADD CONSTRAINT ngblockfit_pkey PRIMARY KEY (iblockid);


--
-- Name: ngdilutionfactor ngdilutionfactor_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngdilutionfactor
    ADD CONSTRAINT ngdilutionfactor_pkey PRIMARY KEY (dilutionid);


--
-- Name: ngextractiondata ngextractiondata_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngextractiondata
    ADD CONSTRAINT ngextractiondata_pkey PRIMARY KEY (extractionid);


--
-- Name: ngextractiondata ngextractiondata_runid_iposition_key; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngextractiondata
    ADD CONSTRAINT ngextractiondata_runid_iposition_key UNIQUE (runid, iposition);


--
-- Name: ngextractionlineefficiency ngextractionlineefficiency_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngextractionlineefficiency
    ADD CONSTRAINT ngextractionlineefficiency_pkey PRIMARY KEY (efficiencyid);


--
-- Name: ngextractionrun ngextractionrun_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngextractionrun
    ADD CONSTRAINT ngextractionrun_pkey PRIMARY KEY (runid);


--
-- Name: nggaugeresult nggaugeresult_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.nggaugeresult
    ADD CONSTRAINT nggaugeresult_pkey PRIMARY KEY (runid, positioninrun, element);


--
-- Name: ngheaders ngheaders_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngheaders
    ADD CONSTRAINT ngheaders_pkey PRIMARY KEY (inobleheaderid);


--
-- Name: nglinearitysnapshots nglinearitysnapshots_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.nglinearitysnapshots
    ADD CONSTRAINT nglinearitysnapshots_pkey PRIMARY KEY (snapshot_id);


--
-- Name: ngpreparationevent ngpreparationevent_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngpreparationevent
    ADD CONSTRAINT ngpreparationevent_pkey PRIMARY KEY (ingpreparationid, ingqualifierid, flvtime);


--
-- Name: ngpreparations ngpreparations_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngpreparations
    ADD CONSTRAINT ngpreparations_pkey PRIMARY KEY (inoblepreparationid);


--
-- Name: ngpreparationstep ngpreparationstep_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngpreparationstep
    ADD CONSTRAINT ngpreparationstep_pkey PRIMARY KEY (ipreparationid, instepqualifier, flvtime);


--
-- Name: ngpreparationstepqualifier ngpreparationstepqualifier_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngpreparationstepqualifier
    ADD CONSTRAINT ngpreparationstepqualifier_pkey PRIMARY KEY (inoblestepqualifierid);


--
-- Name: ngpreparationstepsignal ngpreparationstepsignal_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngpreparationstepsignal
    ADD CONSTRAINT ngpreparationstepsignal_pkey PRIMARY KEY (ipreparationid, instepqualifier, flvtime);


--
-- Name: ngqualifier ngqualifier_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngqualifier
    ADD CONSTRAINT ngqualifier_pkey PRIMARY KEY (inoblequalifierid);


--
-- Name: ngratioresult ngratioresult_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngratioresult
    ADD CONSTRAINT ngratioresult_pkey PRIMARY KEY (runid, positioninrun, ratio_name);


--
-- Name: ngreference ngreference_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngreference
    ADD CONSTRAINT ngreference_pkey PRIMARY KEY (ngreferenceid);


--
-- Name: ngreference ngreference_referencedataid_key; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngreference
    ADD CONSTRAINT ngreference_referencedataid_key UNIQUE (referencedataid);


--
-- Name: ngsequenceevaluation ngsequenceevaluation_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngsequenceevaluation
    ADD CONSTRAINT ngsequenceevaluation_pkey PRIMARY KEY (runid, nvcspecies);


--
-- Name: ngsequencefit ngsequencefit_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngsequencefit
    ADD CONSTRAINT ngsequencefit_pkey PRIMARY KEY (runid, nvcspecies, ccoefficientkind, icoefficientnumber);


--
-- Name: ngsignal ngsignal_pkey; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngsignal
    ADD CONSTRAINT ngsignal_pkey PRIMARY KEY (iblockid, flvtime);


--
-- Name: ngdilutionfactor uq_dilution_factor_slot; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngdilutionfactor
    ADD CONSTRAINT uq_dilution_factor_slot UNIQUE (equipmentid, element, valid_from);


--
-- Name: ngextractionlineefficiency uq_extraction_efficiency_slot; Type: CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngextractionlineefficiency
    ADD CONSTRAINT uq_extraction_efficiency_slot UNIQUE (equipmentid, element, valid_from);


--
-- Name: analysis analysis_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analysis
    ADD CONSTRAINT analysis_pkey PRIMARY KEY (analysisid);


--
-- Name: analysis_repeat analysis_repeat_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analysis_repeat
    ADD CONSTRAINT analysis_repeat_pkey PRIMARY KEY (analysisid, repeat);


--
-- Name: analysisprocedure_measurable analysisprocedure_measurable_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analysisprocedure_measurable
    ADD CONSTRAINT analysisprocedure_measurable_pkey PRIMARY KEY (procedureid, measurableid);


--
-- Name: analysisprocedure analysisprocedure_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analysisprocedure
    ADD CONSTRAINT analysisprocedure_pkey PRIMARY KEY (procedureid);


--
-- Name: analysisprocedure_postprocessing analysisprocedure_postprocessing_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analysisprocedure_postprocessing
    ADD CONSTRAINT analysisprocedure_postprocessing_pkey PRIMARY KEY (id);


--
-- Name: analysisprocedure_template analysisprocedure_template_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analysisprocedure_template
    ADD CONSTRAINT analysisprocedure_template_pkey PRIMARY KEY (procedureid, ordinalposition);


--
-- Name: analytes analytes_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analytes
    ADD CONSTRAINT analytes_pkey PRIMARY KEY (analyteid);


--
-- Name: app_log app_log_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.app_log
    ADD CONSTRAINT app_log_pkey PRIMARY KEY (log_id);


--
-- Name: cims_item cims_item_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cims_item
    ADD CONSTRAINT cims_item_pkey PRIMARY KEY (item_id);


--
-- Name: cims_lot cims_lot_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cims_lot
    ADD CONSTRAINT cims_lot_pkey PRIMARY KEY (lot_id);


--
-- Name: cims_supplier cims_supplier_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cims_supplier
    ADD CONSTRAINT cims_supplier_pkey PRIMARY KEY (supplier_id);


--
-- Name: cims_usage cims_usage_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cims_usage
    ADD CONSTRAINT cims_usage_pkey PRIMARY KEY (usage_id);


--
-- Name: container_type_lookup container_type_lookup_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.container_type_lookup
    ADD CONSTRAINT container_type_lookup_pkey PRIMARY KEY (container_type_id);


--
-- Name: counterinstrument counterinstrument_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.counterinstrument
    ADD CONSTRAINT counterinstrument_pkey PRIMARY KEY (equipmentid);


--
-- Name: country country_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.country
    ADD CONSTRAINT country_pkey PRIMARY KEY (countrycode);


--
-- Name: customer customer_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.customer
    ADD CONSTRAINT customer_pkey PRIMARY KEY (customerid);


--
-- Name: descriptions descriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.descriptions
    ADD CONSTRAINT descriptions_pkey PRIMARY KEY (descriptionid);


--
-- Name: dilutionbatch dilutionbatch_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.dilutionbatch
    ADD CONSTRAINT dilutionbatch_pkey PRIMARY KEY (batchid);


--
-- Name: dilutiondata dilutiondata_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.dilutiondata
    ADD CONSTRAINT dilutiondata_pkey PRIMARY KEY (dilutiondataid);


--
-- Name: distillationprocedure distillationprocedure_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.distillationprocedure
    ADD CONSTRAINT distillationprocedure_pkey PRIMARY KEY (procedureid);


--
-- Name: electrolysiscell electrolysiscell_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.electrolysiscell
    ADD CONSTRAINT electrolysiscell_pkey PRIMARY KEY (cellid);


--
-- Name: electrolysiscellconstant electrolysiscellconstant_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.electrolysiscellconstant
    ADD CONSTRAINT electrolysiscellconstant_pkey PRIMARY KEY (cellconstantid);


--
-- Name: electrolysiscellrecondition electrolysiscellrecondition_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.electrolysiscellrecondition
    ADD CONSTRAINT electrolysiscellrecondition_pkey PRIMARY KEY (id);


--
-- Name: electrolysissystem electrolysissystem_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.electrolysissystem
    ADD CONSTRAINT electrolysissystem_pkey PRIMARY KEY (elyssystemid);


--
-- Name: employee employee_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.employee
    ADD CONSTRAINT employee_pkey PRIMARY KEY (employeeid);


--
-- Name: employee_role employee_role_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.employee_role
    ADD CONSTRAINT employee_role_pkey PRIMARY KEY (id);


--
-- Name: employeemessage employeemessage_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.employeemessage
    ADD CONSTRAINT employeemessage_pkey PRIMARY KEY (id);


--
-- Name: enrichmentprocedure enrichmentprocedure_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.enrichmentprocedure
    ADD CONSTRAINT enrichmentprocedure_pkey PRIMARY KEY (procedureid);


--
-- Name: equipment_job_procedure equipment_job_procedure_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.equipment_job_procedure
    ADD CONSTRAINT equipment_job_procedure_pkey PRIMARY KEY (equipmentid, categoryid);


--
-- Name: equipment_measurables equipment_measurables_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.equipment_measurables
    ADD CONSTRAINT equipment_measurables_pkey PRIMARY KEY (equipmentid, measurableid);


--
-- Name: equipment equipment_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.equipment
    ADD CONSTRAINT equipment_pkey PRIMARY KEY (equipmentid);


--
-- Name: equipment_type equipment_type_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.equipment_type
    ADD CONSTRAINT equipment_type_pkey PRIMARY KEY (typeid);


--
-- Name: equipment_type equipment_type_typename_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.equipment_type
    ADD CONSTRAINT equipment_type_typename_key UNIQUE (typename);


--
-- Name: equipmentmaintenance equipmentmaintenance_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.equipmentmaintenance
    ADD CONSTRAINT equipmentmaintenance_pkey PRIMARY KEY (maintenanceid);


--
-- Name: equipmentmassspec equipmentmassspec_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.equipmentmassspec
    ADD CONSTRAINT equipmentmassspec_pkey PRIMARY KEY (equipmentid);


--
-- Name: finalvalue finalvalue_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.finalvalue
    ADD CONSTRAINT finalvalue_pkey PRIMARY KEY (analysisid, measurableid);


--
-- Name: globalmemo globalmemo_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.globalmemo
    ADD CONSTRAINT globalmemo_pkey PRIMARY KEY (id);


--
-- Name: globalvalue globalvalue_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.globalvalue
    ADD CONSTRAINT globalvalue_pkey PRIMARY KEY (id);


--
-- Name: guitblfileformat guitblfileformat_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.guitblfileformat
    ADD CONSTRAINT guitblfileformat_pkey PRIMARY KEY (lngformatid);


--
-- Name: invoice invoice_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.invoice
    ADD CONSTRAINT invoice_pkey PRIMARY KEY (invoiceid);


--
-- Name: job_procedure job_procedure_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.job_procedure
    ADD CONSTRAINT job_procedure_pkey PRIMARY KEY (id);


--
-- Name: lab_module_config lab_module_config_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.lab_module_config
    ADD CONSTRAINT lab_module_config_pkey PRIMARY KEY (module_key);


--
-- Name: localprintersettings localprintersettings_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.localprintersettings
    ADD CONSTRAINT localprintersettings_pkey PRIMARY KEY (id);


--
-- Name: lscprocedure lscprocedure_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.lscprocedure
    ADD CONSTRAINT lscprocedure_pkey PRIMARY KEY (procedureid);


--
-- Name: ngam_substance materials_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ngam_substance
    ADD CONSTRAINT materials_pkey PRIMARY KEY (substancename);


--
-- Name: matrix matrix_matrixname_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.matrix
    ADD CONSTRAINT matrix_matrixname_key UNIQUE (matrixname);


--
-- Name: matrix matrix_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.matrix
    ADD CONSTRAINT matrix_pkey PRIMARY KEY (matrixid);


--
-- Name: measurables measurables_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.measurables
    ADD CONSTRAINT measurables_pkey PRIMARY KEY (measurableid);


--
-- Name: measurementunit measurementunit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.measurementunit
    ADD CONSTRAINT measurementunit_pkey PRIMARY KEY (unitid);


--
-- Name: media media_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.media
    ADD CONSTRAINT media_pkey PRIMARY KEY (mediaid);


--
-- Name: networktype networktype_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.networktype
    ADD CONSTRAINT networktype_pkey PRIMARY KEY (networktypeid);


--
-- Name: ngextractionprocedure ngextractionprocedure_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ngextractionprocedure
    ADD CONSTRAINT ngextractionprocedure_pkey PRIMARY KEY (procedureid);


--
-- Name: ngseqtemplate ngseqtemplate_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ngseqtemplate
    ADD CONSTRAINT ngseqtemplate_pkey PRIMARY KEY (ingseqtemplateid);


--
-- Name: phaselookup phaselookup_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.phaselookup
    ADD CONSTRAINT phaselookup_pkey PRIMARY KEY (phase);


--
-- Name: chemenrprocedure pk_chemenrprocedure; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.chemenrprocedure
    ADD CONSTRAINT pk_chemenrprocedure PRIMARY KEY (procedureid);


--
-- Name: chemenrsystem pk_chemenrsystem; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.chemenrsystem
    ADD CONSTRAINT pk_chemenrsystem PRIMARY KEY (chemenrsystemid);


--
-- Name: equipmenttrayconfig pk_equipmenttrayconfig; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.equipmenttrayconfig
    ADD CONSTRAINT pk_equipmenttrayconfig PRIMARY KEY (equipmentid);


--
-- Name: priority priority_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.priority
    ADD CONSTRAINT priority_pkey PRIMARY KEY (priorityid);


--
-- Name: privilege privilege_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.privilege
    ADD CONSTRAINT privilege_pkey PRIMARY KEY (privilegekey);


--
-- Name: processing_jobs processing_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.processing_jobs
    ADD CONSTRAINT processing_jobs_pkey PRIMARY KEY (job_id);


--
-- Name: protocol protocol_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.protocol
    ADD CONSTRAINT protocol_pkey PRIMARY KEY (protocolid);


--
-- Name: protocolmapping protocolmapping_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.protocolmapping
    ADD CONSTRAINT protocolmapping_pkey PRIMARY KEY (mappingid);


--
-- Name: reference_source_samples reference_source_samples_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.reference_source_samples
    ADD CONSTRAINT reference_source_samples_pkey PRIMARY KEY (sampleid, prefix);


--
-- Name: referencecontrol referencecontrol_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.referencecontrol
    ADD CONSTRAINT referencecontrol_pkey PRIMARY KEY (referenceid);


--
-- Name: referencecontroldata referencecontroldata_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.referencecontroldata
    ADD CONSTRAINT referencecontroldata_pkey PRIMARY KEY (referencedataid);


--
-- Name: reporting reporting_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.reporting
    ADD CONSTRAINT reporting_pkey PRIMARY KEY (reportid);


--
-- Name: role_module_permission role_module_permission_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.role_module_permission
    ADD CONSTRAINT role_module_permission_pkey PRIMARY KEY (roleid, moduleid);


--
-- Name: role role_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.role
    ADD CONSTRAINT role_pkey PRIMARY KEY (roleid);


--
-- Name: roleprivilege roleprivilege_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.roleprivilege
    ADD CONSTRAINT roleprivilege_pkey PRIMARY KEY (roleid, privilegename);


--
-- Name: runprotocolsnapshot runprotocolsnapshot_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.runprotocolsnapshot
    ADD CONSTRAINT runprotocolsnapshot_pkey PRIMARY KEY (snapshotid);


--
-- Name: sample_duplicate_link sample_duplicate_link_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample_duplicate_link
    ADD CONSTRAINT sample_duplicate_link_pkey PRIMARY KEY (duplicate_sampleid, duplicate_prefix);


--
-- Name: sample_fielddata sample_fielddata_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample_fielddata
    ADD CONSTRAINT sample_fielddata_pkey PRIMARY KEY (sampleid, measurableid);


--
-- Name: sample sample_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample
    ADD CONSTRAINT sample_pkey PRIMARY KEY (sampleid, prefix);


--
-- Name: sample_queue sample_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample_queue
    ADD CONSTRAINT sample_queue_pkey PRIMARY KEY (queue_id);


--
-- Name: sample_queue sample_queue_sampleid_prefix_workflowjobid_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample_queue
    ADD CONSTRAINT sample_queue_sampleid_prefix_workflowjobid_key UNIQUE (sampleid, prefix, workflowjobid);


--
-- Name: samplearchive samplearchive_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.samplearchive
    ADD CONSTRAINT samplearchive_pkey PRIMARY KEY (sampleid, prefix);


--
-- Name: sampletba sampletba_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sampletba
    ADD CONSTRAINT sampletba_pkey PRIMARY KEY (sampletba_id);


--
-- Name: samplingstation samplingstation_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.samplingstation
    ADD CONSTRAINT samplingstation_pkey PRIMARY KEY (stationid);


--
-- Name: samplingstationworkflow samplingstationworkflow_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.samplingstationworkflow
    ADD CONSTRAINT samplingstationworkflow_pkey PRIMARY KEY (id);


--
-- Name: siprocedure siprocedure_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.siprocedure
    ADD CONSTRAINT siprocedure_pkey PRIMARY KEY (procedureid);


--
-- Name: station station_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.station
    ADD CONSTRAINT station_pkey PRIMARY KEY (stationid);


--
-- Name: stationmetadataiaea stationmetadataiaea_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.stationmetadataiaea
    ADD CONSTRAINT stationmetadataiaea_pkey PRIMARY KEY (stationid);


--
-- Name: stationstatushistory stationstatushistory_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.stationstatushistory
    ADD CONSTRAINT stationstatushistory_pkey PRIMARY KEY (id);


--
-- Name: status_label status_label_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.status_label
    ADD CONSTRAINT status_label_pkey PRIMARY KEY (module, status);


--
-- Name: statuslookup statuslookup_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.statuslookup
    ADD CONSTRAINT statuslookup_pkey PRIMARY KEY (status);


--
-- Name: storelocation storelocation_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.storelocation
    ADD CONSTRAINT storelocation_pkey PRIMARY KEY (storelocationid);


--
-- Name: submission submission_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.submission
    ADD CONSTRAINT submission_pkey PRIMARY KEY (submissionid);


--
-- Name: tblreporttemplate tblreporttemplate_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tblreporttemplate
    ADD CONSTRAINT tblreporttemplate_pkey PRIMARY KEY (reporttemplateid);


--
-- Name: tblsampletype tblsampletype_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tblsampletype
    ADD CONSTRAINT tblsampletype_pkey PRIMARY KEY (intsampletype);


--
-- Name: tblstation tblstation_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tblstation
    ADD CONSTRAINT tblstation_pkey PRIMARY KEY (lngstationautoid);


--
-- Name: templatemetadata templatemetadata_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.templatemetadata
    ADD CONSTRAINT templatemetadata_pkey PRIMARY KEY (templateid);


--
-- Name: templatemetadata templatemetadata_procedureid_templatename_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.templatemetadata
    ADD CONSTRAINT templatemetadata_procedureid_templatename_key UNIQUE (procedureid, templatename);


--
-- Name: validation_log validation_log_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.validation_log
    ADD CONSTRAINT validation_log_pkey PRIMARY KEY (logid);


--
-- Name: workflow workflow_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.workflow
    ADD CONSTRAINT workflow_pkey PRIMARY KEY (workflowid);


--
-- Name: workflowjob workflowjob_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.workflowjob
    ADD CONSTRAINT workflowjob_pkey PRIMARY KEY (workflowjobid);


--
-- Name: sianalysiscorrection sianalysiscorrection_pkey; Type: CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysiscorrection
    ADD CONSTRAINT sianalysiscorrection_pkey PRIMARY KEY (sianalysisrunid, correctiontype);


--
-- Name: sianalysiscorrectionfit sianalysiscorrectionfit_pkey; Type: CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysiscorrectionfit
    ADD CONSTRAINT sianalysiscorrectionfit_pkey PRIMARY KEY (id);


--
-- Name: sianalysiscorrectionfitinj sianalysiscorrectionfitinj_pkey; Type: CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysiscorrectionfitinj
    ADD CONSTRAINT sianalysiscorrectionfitinj_pkey PRIMARY KEY (sianalysisrunid, correctiontype, injectionno, measurableid);


--
-- Name: sianalysisinjectiondata sianalysisinjectiondata_pkey; Type: CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysisinjectiondata
    ADD CONSTRAINT sianalysisinjectiondata_pkey PRIMARY KEY (sianalysisid, injectionno);


--
-- Name: sianalysisinterimdata sianalysisinterimdata_pkey; Type: CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysisinterimdata
    ADD CONSTRAINT sianalysisinterimdata_pkey PRIMARY KEY (sianalysisdataid, valueid);


--
-- Name: sianalysisloadlist sianalysisloadlist_pkey; Type: CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysisloadlist
    ADD CONSTRAINT sianalysisloadlist_pkey PRIMARY KEY (sianalysisid);


--
-- Name: sianalysisrawdata sianalysisrawdata_pkey; Type: CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysisrawdata
    ADD CONSTRAINT sianalysisrawdata_pkey PRIMARY KEY (sianalysisdataid);


--
-- Name: sianalysisresult sianalysisresult_pkey; Type: CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysisresult
    ADD CONSTRAINT sianalysisresult_pkey PRIMARY KEY (sianalysisid, measurableid);


--
-- Name: sianalysisrun sianalysisrun_pkey; Type: CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysisrun
    ADD CONSTRAINT sianalysisrun_pkey PRIMARY KEY (sianalysisrunid);


--
-- Name: siinlets siinlets_pkey; Type: CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.siinlets
    ADD CONSTRAINT siinlets_pkey PRIMARY KEY (siid, iinletno);


--
-- Name: simeasurement simeasurement_pkey; Type: CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.simeasurement
    ADD CONSTRAINT simeasurement_pkey PRIMARY KEY (siid);


--
-- Name: deuteriumenrichment deuteriumenrichment_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.deuteriumenrichment
    ADD CONSTRAINT deuteriumenrichment_pkey PRIMARY KEY (deuteriumid);


--
-- Name: electrolysis electrolysis_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.electrolysis
    ADD CONSTRAINT electrolysis_pkey PRIMARY KEY (electrolysisid);


--
-- Name: electrolysisrun electrolysisrun_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.electrolysisrun
    ADD CONSTRAINT electrolysisrun_pkey PRIMARY KEY (runid);


--
-- Name: guitblimportmapping guitblimportmapping_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.guitblimportmapping
    ADD CONSTRAINT guitblimportmapping_pkey PRIMARY KEY (mappingid);


--
-- Name: lscloadlist lscloadlist_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscloadlist
    ADD CONSTRAINT lscloadlist_pkey PRIMARY KEY (countid);


--
-- Name: lscprocedureprotocol lscprocedureprotocol_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscprocedureprotocol
    ADD CONSTRAINT lscprocedureprotocol_pkey PRIMARY KEY (procedureprotocolid);


--
-- Name: lscprotocol lscprotocol_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscprotocol
    ADD CONSTRAINT lscprotocol_pkey PRIMARY KEY (protocolid);


--
-- Name: lscprotocolmapping lscprotocolmapping_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscprotocolmapping
    ADD CONSTRAINT lscprotocolmapping_pkey PRIMARY KEY (mappingid);


--
-- Name: lscprotocolsettings lscprotocolsettings_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscprotocolsettings
    ADD CONSTRAINT lscprotocolsettings_pkey PRIMARY KEY (settingid);


--
-- Name: lscresult lscresult_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscresult
    ADD CONSTRAINT lscresult_pkey PRIMARY KEY (lscresultid);


--
-- Name: lscrun lscrun_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscrun
    ADD CONSTRAINT lscrun_pkey PRIMARY KEY (runid);


--
-- Name: lscrunmean lscrunmean_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscrunmean
    ADD CONSTRAINT lscrunmean_pkey PRIMARY KEY (countid, valuekind);


--
-- Name: lscrunprotocol lscrunprotocol_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscrunprotocol
    ADD CONSTRAINT lscrunprotocol_pkey PRIMARY KEY (runprotocolid);


--
-- Name: lscrunraw lscrunraw_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscrunraw
    ADD CONSTRAINT lscrunraw_pkey PRIMARY KEY (countrawid);


--
-- Name: chemenrrun pk_chemenrrun; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.chemenrrun
    ADD CONSTRAINT pk_chemenrrun PRIMARY KEY (runid);


--
-- Name: chemicalenrichment pk_chemicalenrichment; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.chemicalenrichment
    ADD CONSTRAINT pk_chemicalenrichment PRIMARY KEY (chemenrid);


--
-- Name: lsctrayconfig pk_lsctrayconfig; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lsctrayconfig
    ADD CONSTRAINT pk_lsctrayconfig PRIMARY KEY (equipmentid);


--
-- Name: primarydistillation primarydistillation_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.primarydistillation
    ADD CONSTRAINT primarydistillation_pkey PRIMARY KEY (id);


--
-- Name: primarydistillationbatch primarydistillationbatch_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.primarydistillationbatch
    ADD CONSTRAINT primarydistillationbatch_pkey PRIMARY KEY (runid);


--
-- Name: primarydistillationdata primarydistillationdata_pkey; Type: CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.primarydistillationdata
    ADD CONSTRAINT primarydistillationdata_pkey PRIMARY KEY (distillationdataid);


--
-- Name: ix_amsmeasurement_targetid; Type: INDEX; Schema: ams; Owner: postgres
--

CREATE INDEX ix_amsmeasurement_targetid ON ams.amsmeasurement USING btree (amstargetid);


--
-- Name: ix_amsresult_targetid; Type: INDEX; Schema: ams; Owner: postgres
--

CREATE INDEX ix_amsresult_targetid ON ams.amsresult USING btree (amstargetid);


--
-- Name: ix_amstarget_analysisid; Type: INDEX; Schema: ams; Owner: postgres
--

CREATE INDEX ix_amstarget_analysisid ON ams.amstarget USING btree (analysisid);


--
-- Name: ix_amstarget_graphsampleid; Type: INDEX; Schema: ams; Owner: postgres
--

CREATE INDEX ix_amstarget_graphsampleid ON ams.amstarget USING btree (graphsampleid);


--
-- Name: ix_amstarget_wheelid; Type: INDEX; Schema: ams; Owner: postgres
--

CREATE INDEX ix_amstarget_wheelid ON ams.amstarget USING btree (amswheelid);


--
-- Name: ix_amswheel_runid; Type: INDEX; Schema: ams; Owner: postgres
--

CREATE INDEX ix_amswheel_runid ON ams.amswheel USING btree (amsrunid);


--
-- Name: ix_graphrun_batchdate; Type: INDEX; Schema: ams; Owner: postgres
--

CREATE INDEX ix_graphrun_batchdate ON ams.graphrun USING btree (batchdate DESC);


--
-- Name: ix_graphsample_analysisid; Type: INDEX; Schema: ams; Owner: postgres
--

CREATE INDEX ix_graphsample_analysisid ON ams.graphsample USING btree (analysisid);


--
-- Name: ix_graphsample_runid; Type: INDEX; Schema: ams; Owner: postgres
--

CREATE INDEX ix_graphsample_runid ON ams.graphsample USING btree (graphrunid);


--
-- Name: ix_graphsample_status; Type: INDEX; Schema: ams; Owner: postgres
--

CREATE INDEX ix_graphsample_status ON ams.graphsample USING btree (status) WHERE (isaccepted = true);


--
-- Name: ix_audit_app_user; Type: INDEX; Schema: audit; Owner: postgres
--

CREATE INDEX ix_audit_app_user ON audit.logged_actions USING btree (app_user);


--
-- Name: ix_audit_changed_at; Type: INDEX; Schema: audit; Owner: postgres
--

CREATE INDEX ix_audit_changed_at ON audit.logged_actions USING btree (changed_at DESC);


--
-- Name: ix_audit_operation; Type: INDEX; Schema: audit; Owner: postgres
--

CREATE INDEX ix_audit_operation ON audit.logged_actions USING btree (operation);


--
-- Name: ix_audit_schema_table; Type: INDEX; Schema: audit; Owner: postgres
--

CREATE INDEX ix_audit_schema_table ON audit.logged_actions USING btree (schema_name, table_name);


--
-- Name: idx_dilution_equip_elem_from; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX idx_dilution_equip_elem_from ON ngam.ngdilutionfactor USING btree (equipmentid, element, valid_from DESC);


--
-- Name: idx_dilution_valid_until; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX idx_dilution_valid_until ON ngam.ngdilutionfactor USING btree (valid_until) WHERE (valid_until IS NOT NULL);


--
-- Name: idx_exteff_equip_elem_from; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX idx_exteff_equip_elem_from ON ngam.ngextractionlineefficiency USING btree (equipmentid, element, valid_from DESC);


--
-- Name: idx_exteff_valid_until; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX idx_exteff_valid_until ON ngam.ngextractionlineefficiency USING btree (valid_until) WHERE (valid_until IS NOT NULL);


--
-- Name: idx_msrun_mode; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX idx_msrun_mode ON ngam.msrun USING btree (measurement_mode);


--
-- Name: idx_msrun_starttime; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX idx_msrun_starttime ON ngam.msrun USING btree (runstarttime);


--
-- Name: idx_msrun_workflowid; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX idx_msrun_workflowid ON ngam.msrun USING btree (workflowid);


--
-- Name: idx_ng_cf_template_current; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE UNIQUE INDEX idx_ng_cf_template_current ON ngam.ng_cf_template USING btree (container_type_id, equipmentid) WHERE (is_current = true);


--
-- Name: idx_ng_eqw_run_analysisid; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX idx_ng_eqw_run_analysisid ON ngam.ng_eqw_run USING btree (analysisid);


--
-- Name: idx_ng_eqw_run_ctype_equip; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX idx_ng_eqw_run_ctype_equip ON ngam.ng_eqw_run USING btree (container_type_id, equipmentid);


--
-- Name: idx_ngbgproxyfactor_equip_time; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX idx_ngbgproxyfactor_equip_time ON ngam.ngbgproxyfactor USING btree (equipmentid, computed_at DESC);


--
-- Name: idx_ngextractiondata_analysisid; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX idx_ngextractiondata_analysisid ON ngam.ngextractiondata USING btree (analysisid);


--
-- Name: idx_ngprep_extractionid; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX idx_ngprep_extractionid ON ngam.ngpreparations USING btree (extractionid) WHERE (extractionid IS NOT NULL);


--
-- Name: ix_ng3hesequenceresults_analysisid; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX ix_ng3hesequenceresults_analysisid ON ngam.ng3hesequenceresults USING btree (analysisid);


--
-- Name: ix_ng3hesequenceresults_runid; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX ix_ng3hesequenceresults_runid ON ngam.ng3hesequenceresults USING btree (runid);


--
-- Name: ix_nglinearitysnapshots_ourlabid; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX ix_nglinearitysnapshots_ourlabid ON ngam.nglinearitysnapshots USING btree (ourlabid);


--
-- Name: ix_nglinearitysnapshots_run; Type: INDEX; Schema: ngam; Owner: postgres
--

CREATE INDEX ix_nglinearitysnapshots_run ON ngam.nglinearitysnapshots USING btree (run_id);


--
-- Name: idx_cims_lot_active; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_cims_lot_active ON public.cims_lot USING btree (item_id) WHERE (is_obsolete = 0);


--
-- Name: idx_cims_lot_item; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_cims_lot_item ON public.cims_lot USING btree (item_id);


--
-- Name: idx_cims_usage_date; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_cims_usage_date ON public.cims_usage USING btree (movement_date);


--
-- Name: idx_cims_usage_lot; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_cims_usage_lot ON public.cims_usage USING btree (lot_id);


--
-- Name: idx_cims_usage_run; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_cims_usage_run ON public.cims_usage USING btree (run_module, run_id);


--
-- Name: idx_processing_jobs_run; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_processing_jobs_run ON public.processing_jobs USING btree (run_id, module);


--
-- Name: idx_reference_source_samples_prefix; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_reference_source_samples_prefix ON public.reference_source_samples USING btree (prefix);


--
-- Name: idx_roleprivilege_roleid; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_roleprivilege_roleid ON public.roleprivilege USING btree (roleid);


--
-- Name: idx_sample_container_type; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_sample_container_type ON public.sample USING btree (container_type) WHERE (container_type IS NOT NULL);


--
-- Name: idx_sample_ng_container_type; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_sample_ng_container_type ON public.sample USING btree (ng_container_type) WHERE (ng_container_type IS NOT NULL);


--
-- Name: idx_sample_queue_sample; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_sample_queue_sample ON public.sample_queue USING btree (sampleid, prefix);


--
-- Name: idx_sample_queue_wjid; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_sample_queue_wjid ON public.sample_queue USING btree (workflowjobid);


--
-- Name: idx_sdl_parent; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_sdl_parent ON public.sample_duplicate_link USING btree (parent_sampleid, parent_prefix);


--
-- Name: ix_app_log_level; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_app_log_level ON public.app_log USING btree (level);


--
-- Name: ix_app_log_logged_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_app_log_logged_at ON public.app_log USING btree (logged_at DESC);


--
-- Name: ix_app_log_module; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_app_log_module ON public.app_log USING btree (module);


--
-- Name: ix_measurables_parameterlabel; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_measurables_parameterlabel ON public.measurables USING btree (parameterlabel);


--
-- Name: ix_vallog_analysisid; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vallog_analysisid ON public.validation_log USING btree (analysisid);


--
-- Name: ix_vallog_module_action; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vallog_module_action ON public.validation_log USING btree (module, action);


--
-- Name: ix_vallog_runid; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vallog_runid ON public.validation_log USING btree (runid);


--
-- Name: uq_protocol_name_module; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX uq_protocol_name_module ON public.protocol USING btree (name, module);


--
-- Name: uq_runprotocolsnapshot_modulerun; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX uq_runprotocolsnapshot_modulerun ON public.runprotocolsnapshot USING btree (module, runid);


--
-- Name: ix_chemenr_aid_mid; Type: INDEX; Schema: trims; Owner: postgres
--

CREATE INDEX ix_chemenr_aid_mid ON trims.chemicalenrichment USING btree (analysisid, measurableid);


--
-- Name: ix_chemenr_runid; Type: INDEX; Schema: trims; Owner: postgres
--

CREATE INDEX ix_chemenr_runid ON trims.chemicalenrichment USING btree (runid);


--
-- Name: ix_chemenrrun_measurable_finished; Type: INDEX; Schema: trims; Owner: postgres
--

CREATE INDEX ix_chemenrrun_measurable_finished ON trims.chemenrrun USING btree (measurableid, isfinished);


--
-- Name: analysis trg_audit_Analysis; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_audit_Analysis" AFTER INSERT OR DELETE OR UPDATE ON public.analysis FOR EACH ROW EXECUTE FUNCTION audit.if_modified_func();


--
-- Name: sample trg_audit_Sample; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_audit_Sample" AFTER INSERT OR DELETE OR UPDATE ON public.sample FOR EACH ROW EXECUTE FUNCTION audit.if_modified_func();


--
-- Name: cims_item trg_audit_cims_item; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER trg_audit_cims_item AFTER INSERT OR DELETE OR UPDATE ON public.cims_item FOR EACH ROW EXECUTE FUNCTION audit.if_modified_func();


--
-- Name: cims_lot trg_audit_cims_lot; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER trg_audit_cims_lot AFTER INSERT OR DELETE OR UPDATE ON public.cims_lot FOR EACH ROW EXECUTE FUNCTION audit.if_modified_func();


--
-- Name: cims_supplier trg_audit_cims_supplier; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER trg_audit_cims_supplier AFTER INSERT OR DELETE OR UPDATE ON public.cims_supplier FOR EACH ROW EXECUTE FUNCTION audit.if_modified_func();


--
-- Name: sianalysisresult trg_audit_SIAnalysisResult; Type: TRIGGER; Schema: siam; Owner: postgres
--

CREATE TRIGGER "trg_audit_SIAnalysisResult" AFTER INSERT OR DELETE OR UPDATE ON siam.sianalysisresult FOR EACH ROW EXECUTE FUNCTION audit.if_modified_func();


--
-- Name: sianalysisrun trg_audit_SIAnalysisRun; Type: TRIGGER; Schema: siam; Owner: postgres
--

CREATE TRIGGER "trg_audit_SIAnalysisRun" AFTER INSERT OR DELETE OR UPDATE ON siam.sianalysisrun FOR EACH ROW EXECUTE FUNCTION audit.if_modified_func();


--
-- Name: sianalysisrun trg_sianalysisrun_after_update; Type: TRIGGER; Schema: siam; Owner: postgres
--

CREATE TRIGGER trg_sianalysisrun_after_update AFTER UPDATE OF runstatus, runendtime ON siam.sianalysisrun FOR EACH ROW WHEN (((old.runstatus IS DISTINCT FROM new.runstatus) OR (old.runendtime IS DISTINCT FROM new.runendtime))) EXECUTE FUNCTION public.trg_sianalysisrun_phase_sync();


--
-- Name: electrolysis trg_audit_Electrolysis; Type: TRIGGER; Schema: trims; Owner: postgres
--

CREATE TRIGGER "trg_audit_Electrolysis" AFTER INSERT OR DELETE OR UPDATE ON trims.electrolysis FOR EACH ROW EXECUTE FUNCTION audit.if_modified_func();


--
-- Name: electrolysisrun trg_audit_ElectrolysisRun; Type: TRIGGER; Schema: trims; Owner: postgres
--

CREATE TRIGGER "trg_audit_ElectrolysisRun" AFTER INSERT OR DELETE OR UPDATE ON trims.electrolysisrun FOR EACH ROW EXECUTE FUNCTION audit.if_modified_func();


--
-- Name: lscloadlist trg_audit_LSCLoadList; Type: TRIGGER; Schema: trims; Owner: postgres
--

CREATE TRIGGER "trg_audit_LSCLoadList" AFTER INSERT OR DELETE OR UPDATE ON trims.lscloadlist FOR EACH ROW EXECUTE FUNCTION audit.if_modified_func();


--
-- Name: lscresult trg_audit_LSCResult; Type: TRIGGER; Schema: trims; Owner: postgres
--

CREATE TRIGGER "trg_audit_LSCResult" AFTER INSERT OR DELETE OR UPDATE ON trims.lscresult FOR EACH ROW EXECUTE FUNCTION audit.if_modified_func();


--
-- Name: lscrun trg_audit_LSCRun; Type: TRIGGER; Schema: trims; Owner: postgres
--

CREATE TRIGGER "trg_audit_LSCRun" AFTER INSERT OR DELETE OR UPDATE ON trims.lscrun FOR EACH ROW EXECUTE FUNCTION audit.if_modified_func();


--
-- Name: primarydistillationbatch trg_distillationbatch_after_update; Type: TRIGGER; Schema: trims; Owner: postgres
--

CREATE TRIGGER trg_distillationbatch_after_update AFTER UPDATE OF enddate ON trims.primarydistillationbatch FOR EACH ROW WHEN (((old.enddate IS DISTINCT FROM new.enddate) AND (new.enddate IS NOT NULL))) EXECUTE FUNCTION public.trg_distillationbatch_phase_sync();


--
-- Name: electrolysisrun trg_electrolysisrun_after_update; Type: TRIGGER; Schema: trims; Owner: postgres
--

CREATE TRIGGER trg_electrolysisrun_after_update AFTER UPDATE OF runstatus, runendtime ON trims.electrolysisrun FOR EACH ROW WHEN (((old.runstatus IS DISTINCT FROM new.runstatus) OR (old.runendtime IS DISTINCT FROM new.runendtime))) EXECUTE FUNCTION public.trg_electrolysisrun_phase_sync();


--
-- Name: lscrun trg_lscrun_after_update; Type: TRIGGER; Schema: trims; Owner: postgres
--

CREATE TRIGGER trg_lscrun_after_update AFTER UPDATE OF runstatus, runendtime ON trims.lscrun FOR EACH ROW WHEN (((old.runstatus IS DISTINCT FROM new.runstatus) OR (old.runendtime IS DISTINCT FROM new.runendtime))) EXECUTE FUNCTION public.trg_lscrun_phase_sync();


--
-- Name: amsmeasurement fk_amsmeas_target; Type: FK CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amsmeasurement
    ADD CONSTRAINT fk_amsmeas_target FOREIGN KEY (amstargetid) REFERENCES ams.amstarget(amstargetid) ON DELETE CASCADE;


--
-- Name: amsresult fk_amsresult_target; Type: FK CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amsresult
    ADD CONSTRAINT fk_amsresult_target FOREIGN KEY (amstargetid) REFERENCES ams.amstarget(amstargetid) ON DELETE CASCADE;


--
-- Name: amsrun fk_amsrun_equipment; Type: FK CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amsrun
    ADD CONSTRAINT fk_amsrun_equipment FOREIGN KEY (equipmentid) REFERENCES public.equipment(equipmentid);


--
-- Name: amsrun fk_amsrun_technician; Type: FK CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amsrun
    ADD CONSTRAINT fk_amsrun_technician FOREIGN KEY (technicianid) REFERENCES public.employee(employeeid);


--
-- Name: amstarget fk_amstarget_analysis; Type: FK CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amstarget
    ADD CONSTRAINT fk_amstarget_analysis FOREIGN KEY (analysisid) REFERENCES public.analysis(analysisid);


--
-- Name: amstarget fk_amstarget_graphsample; Type: FK CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amstarget
    ADD CONSTRAINT fk_amstarget_graphsample FOREIGN KEY (graphsampleid) REFERENCES ams.graphsample(graphsampleid);


--
-- Name: amstarget fk_amstarget_wheel; Type: FK CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amstarget
    ADD CONSTRAINT fk_amstarget_wheel FOREIGN KEY (amswheelid) REFERENCES ams.amswheel(amswheelid) ON DELETE CASCADE;


--
-- Name: amswheel fk_amswheel_run; Type: FK CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.amswheel
    ADD CONSTRAINT fk_amswheel_run FOREIGN KEY (amsrunid) REFERENCES ams.amsrun(amsrunid) ON DELETE CASCADE;


--
-- Name: graphrun fk_graphrun_equipment; Type: FK CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.graphrun
    ADD CONSTRAINT fk_graphrun_equipment FOREIGN KEY (equipmentid) REFERENCES public.equipment(equipmentid);


--
-- Name: graphrun fk_graphrun_technician; Type: FK CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.graphrun
    ADD CONSTRAINT fk_graphrun_technician FOREIGN KEY (technicianid) REFERENCES public.employee(employeeid);


--
-- Name: graphsample fk_graphsample_analysis; Type: FK CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.graphsample
    ADD CONSTRAINT fk_graphsample_analysis FOREIGN KEY (analysisid) REFERENCES public.analysis(analysisid);


--
-- Name: graphsample fk_graphsample_run; Type: FK CONSTRAINT; Schema: ams; Owner: postgres
--

ALTER TABLE ONLY ams.graphsample
    ADD CONSTRAINT fk_graphsample_run FOREIGN KEY (graphrunid) REFERENCES ams.graphrun(graphrunid);


--
-- Name: data chemistrydata$chemistrydataloadlist; Type: FK CONSTRAINT; Schema: chem; Owner: postgres
--

ALTER TABLE ONLY chem.data
    ADD CONSTRAINT "chemistrydata$chemistrydataloadlist" FOREIGN KEY (chemanalysisid) REFERENCES chem.loadlist(chemanalysisid);


--
-- Name: loadlist chemistryloadlist$analysischemistryloadlist; Type: FK CONSTRAINT; Schema: chem; Owner: postgres
--

ALTER TABLE ONLY chem.loadlist
    ADD CONSTRAINT "chemistryloadlist$analysischemistryloadlist" FOREIGN KEY (analysisid) REFERENCES public.analysis(analysisid);


--
-- Name: loadlist chemistryloadlist$chemistrybatchloadlist; Type: FK CONSTRAINT; Schema: chem; Owner: postgres
--

ALTER TABLE ONLY chem.loadlist
    ADD CONSTRAINT "chemistryloadlist$chemistrybatchloadlist" FOREIGN KEY (runid) REFERENCES chem.batch(runid);


--
-- Name: ng3hesequenceresults fk_ng3hesequenceresults_unitid; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng3hesequenceresults
    ADD CONSTRAINT fk_ng3hesequenceresults_unitid FOREIGN KEY (unitid) REFERENCES public.measurementunit(unitid);


--
-- Name: ngpreparations fk_ngprep_extractionid; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngpreparations
    ADD CONSTRAINT fk_ngprep_extractionid FOREIGN KEY (extractionid) REFERENCES ngam.ngextractiondata(extractionid) NOT VALID;


--
-- Name: msrun msrun_procedureid_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.msrun
    ADD CONSTRAINT msrun_procedureid_fkey FOREIGN KEY (procedureid) REFERENCES public.analysisprocedure(procedureid);


--
-- Name: ng3hesequenceloadlist ng3hesequenceloadlist_knownstdactivityunitid_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng3hesequenceloadlist
    ADD CONSTRAINT ng3hesequenceloadlist_knownstdactivityunitid_fkey FOREIGN KEY (knownstdactivityunitid) REFERENCES public.measurementunit(unitid);


--
-- Name: ng_cf_template ng_cf_template_container_type_id_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_cf_template
    ADD CONSTRAINT ng_cf_template_container_type_id_fkey FOREIGN KEY (container_type_id) REFERENCES public.container_type_lookup(container_type_id);


--
-- Name: ng_cf_template ng_cf_template_equipmentid_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_cf_template
    ADD CONSTRAINT ng_cf_template_equipmentid_fkey FOREIGN KEY (equipmentid) REFERENCES public.equipment(equipmentid);


--
-- Name: ng_cf_template_run ng_cf_template_run_eqw_run_id_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_cf_template_run
    ADD CONSTRAINT ng_cf_template_run_eqw_run_id_fkey FOREIGN KEY (eqw_run_id) REFERENCES ngam.ng_eqw_run(eqw_run_id) ON DELETE CASCADE;


--
-- Name: ng_cf_template_run ng_cf_template_run_template_id_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_cf_template_run
    ADD CONSTRAINT ng_cf_template_run_template_id_fkey FOREIGN KEY (template_id) REFERENCES ngam.ng_cf_template(template_id) ON DELETE CASCADE;


--
-- Name: ng_eqw_run ng_eqw_run_analysisid_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_eqw_run
    ADD CONSTRAINT ng_eqw_run_analysisid_fkey FOREIGN KEY (analysisid) REFERENCES public.analysis(analysisid) ON DELETE SET NULL;


--
-- Name: ng_eqw_run ng_eqw_run_container_type_id_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_eqw_run
    ADD CONSTRAINT ng_eqw_run_container_type_id_fkey FOREIGN KEY (container_type_id) REFERENCES public.container_type_lookup(container_type_id);


--
-- Name: ng_eqw_run ng_eqw_run_equipmentid_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng_eqw_run
    ADD CONSTRAINT ng_eqw_run_equipmentid_fkey FOREIGN KEY (equipmentid) REFERENCES public.equipment(equipmentid);


--
-- Name: ngblockevaluation ngblockevaluation_iblockid_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngblockevaluation
    ADD CONSTRAINT ngblockevaluation_iblockid_fkey FOREIGN KEY (iblockid) REFERENCES ngam.ngblock(iblockid) ON DELETE CASCADE;


--
-- Name: ngblockfit ngblockfit_iblockid_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngblockfit
    ADD CONSTRAINT ngblockfit_iblockid_fkey FOREIGN KEY (iblockid) REFERENCES ngam.ngblock(iblockid) ON DELETE CASCADE;


--
-- Name: ngdilutionfactor ngdilutionfactor_equipmentid_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngdilutionfactor
    ADD CONSTRAINT ngdilutionfactor_equipmentid_fkey FOREIGN KEY (equipmentid) REFERENCES public.equipment(equipmentid);


--
-- Name: ngdilutionfactor ngdilutionfactor_msrun_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngdilutionfactor
    ADD CONSTRAINT ngdilutionfactor_msrun_fkey FOREIGN KEY (runid) REFERENCES ngam.msrun(runid);


--
-- Name: ngextractionlineefficiency ngextractionlineefficiency_equipmentid_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngextractionlineefficiency
    ADD CONSTRAINT ngextractionlineefficiency_equipmentid_fkey FOREIGN KEY (equipmentid) REFERENCES public.equipment(equipmentid);


--
-- Name: ngextractionlineefficiency ngextractionlineefficiency_runid_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngextractionlineefficiency
    ADD CONSTRAINT ngextractionlineefficiency_runid_fkey FOREIGN KEY (runid) REFERENCES ngam.ngextractionrun(runid);


--
-- Name: ngheaders ngheaders_msrun_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngheaders
    ADD CONSTRAINT ngheaders_msrun_fkey FOREIGN KEY (runid) REFERENCES ngam.msrun(runid);


--
-- Name: ngpreparations ngpreparations_msrun_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngpreparations
    ADD CONSTRAINT ngpreparations_msrun_fkey FOREIGN KEY (runid) REFERENCES ngam.msrun(runid);


--
-- Name: ngreference ngreference_referencedataid_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngreference
    ADD CONSTRAINT ngreference_referencedataid_fkey FOREIGN KEY (referencedataid) REFERENCES public.referencecontroldata(referencedataid) ON DELETE SET NULL;


--
-- Name: ngsequenceevaluation ngsequenceevaluation_msrun_fkey; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngsequenceevaluation
    ADD CONSTRAINT ngsequenceevaluation_msrun_fkey FOREIGN KEY (runid) REFERENCES ngam.msrun(runid);


--
-- Name: ngblock tblnobleblocks$tblnobleheaderstblnobleblocks; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngblock
    ADD CONSTRAINT "tblnobleblocks$tblnobleheaderstblnobleblocks" FOREIGN KEY (iheaderid) REFERENCES ngam.ngheaders(inobleheaderid);


--
-- Name: ng3heingrowthdata tblnoblegasextractions$analysistblnoblegasextractions; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng3heingrowthdata
    ADD CONSTRAINT "tblnoblegasextractions$analysistblnoblegasextractions" FOREIGN KEY (analysisid) REFERENCES public.analysis(analysisid);


--
-- Name: ng3heingrowthdata tblnoblegasextractions$tblnoblegasextractionrunstblnoblegasextr; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ng3heingrowthdata
    ADD CONSTRAINT "tblnoblegasextractions$tblnoblegasextractionrunstblnoblegasextr" FOREIGN KEY (runid) REFERENCES ngam.ng3heingrowthrun(runid);


--
-- Name: ngheaders tblnobleheaders$tblnoblepreparationstblnobleheaders; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngheaders
    ADD CONSTRAINT "tblnobleheaders$tblnoblepreparationstblnobleheaders" FOREIGN KEY (ipreparationid) REFERENCES ngam.ngpreparations(inoblepreparationid);


--
-- Name: ngpreparationevent tblnoblepreparationevents$tblnoblepreparationstblnoblepreparati; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngpreparationevent
    ADD CONSTRAINT "tblnoblepreparationevents$tblnoblepreparationstblnoblepreparati" FOREIGN KEY (ingpreparationid) REFERENCES ngam.ngpreparations(inoblepreparationid);


--
-- Name: ngpreparationevent tblnoblepreparationevents$tblnoblequalifiertblnoblepreparatione; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngpreparationevent
    ADD CONSTRAINT "tblnoblepreparationevents$tblnoblequalifiertblnoblepreparatione" FOREIGN KEY (ingqualifierid) REFERENCES ngam.ngqualifier(inoblequalifierid);


--
-- Name: ngpreparations tblnoblepreparations$analysistblnoblepreparations; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngpreparations
    ADD CONSTRAINT "tblnoblepreparations$analysistblnoblepreparations" FOREIGN KEY (analysisid) REFERENCES public.analysis(analysisid);


--
-- Name: ngpreparationstep tblnoblepreparationsteps$tblnoblepreparationstblnoblepreparatio; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngpreparationstep
    ADD CONSTRAINT "tblnoblepreparationsteps$tblnoblepreparationstblnoblepreparatio" FOREIGN KEY (ipreparationid) REFERENCES ngam.ngpreparations(inoblepreparationid);


--
-- Name: ngpreparationstep tblnoblepreparationsteps$tblnoblepreparationstepqualiftblnoblep; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngpreparationstep
    ADD CONSTRAINT "tblnoblepreparationsteps$tblnoblepreparationstepqualiftblnoblep" FOREIGN KEY (instepqualifier) REFERENCES ngam.ngpreparationstepqualifier(inoblestepqualifierid);


--
-- Name: ngpreparationstepsignal tblnoblepreparationstepsignals$tblnoblepreparationstblnobleprep; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngpreparationstepsignal
    ADD CONSTRAINT "tblnoblepreparationstepsignals$tblnoblepreparationstblnobleprep" FOREIGN KEY (ipreparationid) REFERENCES ngam.ngpreparations(inoblepreparationid);


--
-- Name: ngsequencefit tblnoblesequencefits$tblnoblesequenceevaluationstblnoblesequenc; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngsequencefit
    ADD CONSTRAINT "tblnoblesequencefits$tblnoblesequenceevaluationstblnoblesequenc" FOREIGN KEY (runid, nvcspecies) REFERENCES ngam.ngsequenceevaluation(runid, nvcspecies);


--
-- Name: ngsignal tblnoblesignals$tblnobleblockstblnoblesignals; Type: FK CONSTRAINT; Schema: ngam; Owner: postgres
--

ALTER TABLE ONLY ngam.ngsignal
    ADD CONSTRAINT "tblnoblesignals$tblnobleblockstblnoblesignals" FOREIGN KEY (iblockid) REFERENCES ngam.ngblock(iblockid);


--
-- Name: analysisprocedure_measurable analysisprocedure_measurable$analysisprocedure; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analysisprocedure_measurable
    ADD CONSTRAINT "analysisprocedure_measurable$analysisprocedure" FOREIGN KEY (procedureid) REFERENCES public.analysisprocedure(procedureid);


--
-- Name: analysisprocedure_template analysisprocedure_template_sampletype_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analysisprocedure_template
    ADD CONSTRAINT analysisprocedure_template_sampletype_fkey FOREIGN KEY (sampletype) REFERENCES public.tblsampletype(intsampletype);


--
-- Name: analysisprocedure_template analysisprocedure_template_templateid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analysisprocedure_template
    ADD CONSTRAINT analysisprocedure_template_templateid_fkey FOREIGN KEY (templateid) REFERENCES public.templatemetadata(templateid);


--
-- Name: analytes analytes_descriptionid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analytes
    ADD CONSTRAINT analytes_descriptionid_fkey FOREIGN KEY (descriptionid) REFERENCES public.descriptions(descriptionid);


--
-- Name: analytes analytes_matrixid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analytes
    ADD CONSTRAINT analytes_matrixid_fkey FOREIGN KEY (matrixid) REFERENCES public.matrix(matrixid);


--
-- Name: chemenrprocedure chemenrprocedure_measurableid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.chemenrprocedure
    ADD CONSTRAINT chemenrprocedure_measurableid_fkey FOREIGN KEY (measurableid) REFERENCES public.analytes(analyteid);


--
-- Name: cims_lot cims_lot_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cims_lot
    ADD CONSTRAINT cims_lot_item_id_fkey FOREIGN KEY (item_id) REFERENCES public.cims_item(item_id);


--
-- Name: cims_lot cims_lot_supplier_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cims_lot
    ADD CONSTRAINT cims_lot_supplier_id_fkey FOREIGN KEY (supplier_id) REFERENCES public.cims_supplier(supplier_id);


--
-- Name: cims_usage cims_usage_lot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cims_usage
    ADD CONSTRAINT cims_usage_lot_id_fkey FOREIGN KEY (lot_id) REFERENCES public.cims_lot(lot_id);


--
-- Name: counterinstrument counterinstrument$equipmentcounterinstrument; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.counterinstrument
    ADD CONSTRAINT "counterinstrument$equipmentcounterinstrument" FOREIGN KEY (equipmentid) REFERENCES public.equipment(equipmentid);


--
-- Name: customer customer$tblcountrytblcustomer; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.customer
    ADD CONSTRAINT "customer$tblcountrytblcustomer" FOREIGN KEY (countrycode) REFERENCES public.country(countrycode);


--
-- Name: distillationprocedure distillationprocedure$analysisproceduredistillationprocedure; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.distillationprocedure
    ADD CONSTRAINT "distillationprocedure$analysisproceduredistillationprocedure" FOREIGN KEY (procedureid) REFERENCES public.analysisprocedure(procedureid);


--
-- Name: electrolysiscell electrolysiscell$electrolysissystemelectrolysiscell; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.electrolysiscell
    ADD CONSTRAINT "electrolysiscell$electrolysissystemelectrolysiscell" FOREIGN KEY (systemid) REFERENCES public.electrolysissystem(elyssystemid);


--
-- Name: electrolysiscellconstant electrolysiscellconstant$tblelectrolysiscelltblelectrolysiscell; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.electrolysiscellconstant
    ADD CONSTRAINT "electrolysiscellconstant$tblelectrolysiscelltblelectrolysiscell" FOREIGN KEY (cellid) REFERENCES public.electrolysiscell(cellid);


--
-- Name: electrolysiscellrecondition electrolysiscellrecondition_cell; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.electrolysiscellrecondition
    ADD CONSTRAINT electrolysiscellrecondition_cell FOREIGN KEY (cellid) REFERENCES public.electrolysiscell(cellid);


--
-- Name: electrolysissystem electrolysissystem$equipmentelectrolysissystem; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.electrolysissystem
    ADD CONSTRAINT "electrolysissystem$equipmentelectrolysissystem" FOREIGN KEY (elyssystemid) REFERENCES public.equipment(equipmentid);


--
-- Name: employee_role employee_role$employeeemployee_role; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.employee_role
    ADD CONSTRAINT "employee_role$employeeemployee_role" FOREIGN KEY (employeeid) REFERENCES public.employee(employeeid);


--
-- Name: employee_role employee_role$roleemployee_role; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.employee_role
    ADD CONSTRAINT "employee_role$roleemployee_role" FOREIGN KEY (roleid) REFERENCES public.role(roleid);


--
-- Name: employeemessage employeemessage_recipientid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.employeemessage
    ADD CONSTRAINT employeemessage_recipientid_fkey FOREIGN KEY (recipientid) REFERENCES public.employee(employeeid);


--
-- Name: employeemessage employeemessage_senderid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.employeemessage
    ADD CONSTRAINT employeemessage_senderid_fkey FOREIGN KEY (senderid) REFERENCES public.employee(employeeid);


--
-- Name: enrichmentprocedure enrichmentprocedure$analysisprocedureenrichmentprocedure; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.enrichmentprocedure
    ADD CONSTRAINT "enrichmentprocedure$analysisprocedureenrichmentprocedure" FOREIGN KEY (procedureid) REFERENCES public.analysisprocedure(procedureid);


--
-- Name: equipment_job_procedure equipment_job_procedure_categoryid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.equipment_job_procedure
    ADD CONSTRAINT equipment_job_procedure_categoryid_fkey FOREIGN KEY (categoryid) REFERENCES public.job_procedure(id);


--
-- Name: equipment_job_procedure equipment_job_procedure_equipmentid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.equipment_job_procedure
    ADD CONSTRAINT equipment_job_procedure_equipmentid_fkey FOREIGN KEY (equipmentid) REFERENCES public.equipment(equipmentid) ON DELETE CASCADE;


--
-- Name: equipment_measurables equipment_measurables$equipmentequipment_measurables; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.equipment_measurables
    ADD CONSTRAINT "equipment_measurables$equipmentequipment_measurables" FOREIGN KEY (equipmentid) REFERENCES public.equipment(equipmentid);


--
-- Name: equipment equipment_typeid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.equipment
    ADD CONSTRAINT equipment_typeid_fkey FOREIGN KEY (typeid) REFERENCES public.equipment_type(typeid);


--
-- Name: equipmentmaintenance equipmentmaintenance$equipmentequipmentmaintenance; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.equipmentmaintenance
    ADD CONSTRAINT "equipmentmaintenance$equipmentequipmentmaintenance" FOREIGN KEY (equipmentid) REFERENCES public.equipment(equipmentid);


--
-- Name: finalvalue finalvalue$analysisfinalvalue; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.finalvalue
    ADD CONSTRAINT "finalvalue$analysisfinalvalue" FOREIGN KEY (analysisid) REFERENCES public.analysis(analysisid);


--
-- Name: analysis_repeat fk_analysis_repeat_analysis; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analysis_repeat
    ADD CONSTRAINT fk_analysis_repeat_analysis FOREIGN KEY (analysisid) REFERENCES public.analysis(analysisid);


--
-- Name: analysis fk_analysis_sample; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analysis
    ADD CONSTRAINT fk_analysis_sample FOREIGN KEY (sampleid, prefix) REFERENCES public.sample(sampleid, prefix);


--
-- Name: chemenrprocedure fk_chemenrprocedure_procedure; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.chemenrprocedure
    ADD CONSTRAINT fk_chemenrprocedure_procedure FOREIGN KEY (procedureid) REFERENCES public.analysisprocedure(procedureid) ON DELETE CASCADE;


--
-- Name: chemenrsystem fk_chemenrsystem_equipment; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.chemenrsystem
    ADD CONSTRAINT fk_chemenrsystem_equipment FOREIGN KEY (chemenrsystemid) REFERENCES public.equipment(equipmentid) ON DELETE CASCADE;


--
-- Name: dilutiondata fk_dilutiondata_dilutionbatch; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.dilutiondata
    ADD CONSTRAINT fk_dilutiondata_dilutionbatch FOREIGN KEY (batchid) REFERENCES public.dilutionbatch(batchid);


--
-- Name: dilutiondata fk_dilutiondata_sample; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.dilutiondata
    ADD CONSTRAINT fk_dilutiondata_sample FOREIGN KEY (sampleid, prefix) REFERENCES public.sample(sampleid, prefix);


--
-- Name: equipmenttrayconfig fk_equipmenttrayconfig_equipment; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.equipmenttrayconfig
    ADD CONSTRAINT fk_equipmenttrayconfig_equipment FOREIGN KEY (equipmentid) REFERENCES public.equipment(equipmentid);


--
-- Name: protocolmapping fk_protocolmapping_protocol; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.protocolmapping
    ADD CONSTRAINT fk_protocolmapping_protocol FOREIGN KEY (protocolid) REFERENCES public.protocol(protocolid);


--
-- Name: reporting fk_reporting_media; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.reporting
    ADD CONSTRAINT fk_reporting_media FOREIGN KEY (mediaid) REFERENCES public.media(mediaid);


--
-- Name: roleprivilege fk_roleprivilege_privilege; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.roleprivilege
    ADD CONSTRAINT fk_roleprivilege_privilege FOREIGN KEY (privilegename) REFERENCES public.privilege(privilegekey) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: runprotocolsnapshot fk_runprotocolsnapshot_protocol; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.runprotocolsnapshot
    ADD CONSTRAINT fk_runprotocolsnapshot_protocol FOREIGN KEY (protocolid) REFERENCES public.protocol(protocolid);


--
-- Name: sample_fielddata fk_sample_fielddata_sample; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample_fielddata
    ADD CONSTRAINT fk_sample_fielddata_sample FOREIGN KEY (sampleid, prefix) REFERENCES public.sample(sampleid, prefix);


--
-- Name: samplearchive fk_samplearchive_sample; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.samplearchive
    ADD CONSTRAINT fk_samplearchive_sample FOREIGN KEY (sampleid, prefix) REFERENCES public.sample(sampleid, prefix);


--
-- Name: sample_queue fk_sq_sample; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample_queue
    ADD CONSTRAINT fk_sq_sample FOREIGN KEY (sampleid, prefix) REFERENCES public.sample(sampleid, prefix) ON DELETE CASCADE NOT VALID;


--
-- Name: sample_queue fk_sq_workflowjob; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample_queue
    ADD CONSTRAINT fk_sq_workflowjob FOREIGN KEY (workflowjobid) REFERENCES public.workflowjob(workflowjobid) NOT VALID;


--
-- Name: station fk_station_networktype; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.station
    ADD CONSTRAINT fk_station_networktype FOREIGN KEY (networktypeid) REFERENCES public.networktype(networktypeid);


--
-- Name: stationmetadataiaea fk_stationmetadataiaea_samplingstation; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.stationmetadataiaea
    ADD CONSTRAINT fk_stationmetadataiaea_samplingstation FOREIGN KEY (stationid) REFERENCES public.samplingstation(stationid);


--
-- Name: stationstatushistory fk_stationstatushistory_samplingstation; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.stationstatushistory
    ADD CONSTRAINT fk_stationstatushistory_samplingstation FOREIGN KEY (stationid) REFERENCES public.samplingstation(stationid);


--
-- Name: validation_log fk_vallog_employee; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.validation_log
    ADD CONSTRAINT fk_vallog_employee FOREIGN KEY (employeeid) REFERENCES public.employee(employeeid) ON DELETE SET NULL;


--
-- Name: invoice invoice$tblprojecttblinvoice; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.invoice
    ADD CONSTRAINT "invoice$tblprojecttblinvoice" FOREIGN KEY (submissionid) REFERENCES public.submission(submissionid);


--
-- Name: lscprocedure lscprocedure$analysisprocedurelscprocedure; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.lscprocedure
    ADD CONSTRAINT "lscprocedure$analysisprocedurelscprocedure" FOREIGN KEY (procedureid) REFERENCES public.analysisprocedure(procedureid);


--
-- Name: ngextractionprocedure ngextractionprocedure$analysisprocedurengextractionprocedure; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ngextractionprocedure
    ADD CONSTRAINT "ngextractionprocedure$analysisprocedurengextractionprocedure" FOREIGN KEY (procedureid) REFERENCES public.analysisprocedure(procedureid);


--
-- Name: ngseqtemplate ngseqtemplate$analysisprocedure; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ngseqtemplate
    ADD CONSTRAINT "ngseqtemplate$analysisprocedure" FOREIGN KEY (procedureid) REFERENCES public.analysisprocedure(procedureid);


--
-- Name: analysisprocedure_postprocessing procedure_postprocessing; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analysisprocedure_postprocessing
    ADD CONSTRAINT procedure_postprocessing FOREIGN KEY (procedureid) REFERENCES public.analysisprocedure(procedureid);


--
-- Name: reference_source_samples reference_source_samples_sampleid_prefix_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.reference_source_samples
    ADD CONSTRAINT reference_source_samples_sampleid_prefix_fkey FOREIGN KEY (sampleid, prefix) REFERENCES public.sample(sampleid, prefix) ON DELETE CASCADE;


--
-- Name: referencecontroldata referencecontroldata$tblsireferencetblsireferencedata; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.referencecontroldata
    ADD CONSTRAINT "referencecontroldata$tblsireferencetblsireferencedata" FOREIGN KEY (referenceid) REFERENCES public.referencecontrol(referenceid);


--
-- Name: reporting reporting$tblprojecttblreport; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.reporting
    ADD CONSTRAINT "reporting$tblprojecttblreport" FOREIGN KEY (submissionid) REFERENCES public.submission(submissionid);


--
-- Name: reporting reporting$tblreporttemplatereporting; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.reporting
    ADD CONSTRAINT "reporting$tblreporttemplatereporting" FOREIGN KEY (reporttemplateid) REFERENCES public.tblreporttemplate(reporttemplateid);


--
-- Name: role_module_permission role_module_permission_roleid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.role_module_permission
    ADD CONSTRAINT role_module_permission_roleid_fkey FOREIGN KEY (roleid) REFERENCES public.role(roleid) ON DELETE CASCADE;


--
-- Name: roleprivilege roleprivilege_roleid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.roleprivilege
    ADD CONSTRAINT roleprivilege_roleid_fkey FOREIGN KEY (roleid) REFERENCES public.role(roleid) ON DELETE CASCADE;


--
-- Name: sample sample$countrysample; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample
    ADD CONSTRAINT "sample$countrysample" FOREIGN KEY (countrycode) REFERENCES public.country(countrycode);


--
-- Name: sample sample$tblmediatblsample; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample
    ADD CONSTRAINT "sample$tblmediatblsample" FOREIGN KEY (mediaid) REFERENCES public.media(mediaid);


--
-- Name: sample sample$tblprojecttblsample1; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample
    ADD CONSTRAINT "sample$tblprojecttblsample1" FOREIGN KEY (submissionid) REFERENCES public.submission(submissionid);


--
-- Name: sample sample$tblsampletypesample; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample
    ADD CONSTRAINT "sample$tblsampletypesample" FOREIGN KEY (sampletype) REFERENCES public.tblsampletype(intsampletype);


--
-- Name: sample sample_container_type_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample
    ADD CONSTRAINT sample_container_type_fkey FOREIGN KEY (container_type) REFERENCES public.container_type_lookup(container_type_id);


--
-- Name: sample_duplicate_link sample_duplicate_link_duplicate_sampleid_duplicate_prefix_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample_duplicate_link
    ADD CONSTRAINT sample_duplicate_link_duplicate_sampleid_duplicate_prefix_fkey FOREIGN KEY (duplicate_sampleid, duplicate_prefix) REFERENCES public.sample(sampleid, prefix) ON DELETE CASCADE;


--
-- Name: sample_duplicate_link sample_duplicate_link_parent_sampleid_parent_prefix_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample_duplicate_link
    ADD CONSTRAINT sample_duplicate_link_parent_sampleid_parent_prefix_fkey FOREIGN KEY (parent_sampleid, parent_prefix) REFERENCES public.sample(sampleid, prefix) ON DELETE CASCADE;


--
-- Name: sample sample_ng_container_type_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sample
    ADD CONSTRAINT sample_ng_container_type_fkey FOREIGN KEY (ng_container_type) REFERENCES public.container_type_lookup(container_type_id);


--
-- Name: sampletba sampletba$workflowsampletba; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sampletba
    ADD CONSTRAINT "sampletba$workflowsampletba" FOREIGN KEY (workflowid) REFERENCES public.workflow(workflowid);


--
-- Name: siprocedure siprocedure$analysisproceduresiprocedure; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.siprocedure
    ADD CONSTRAINT "siprocedure$analysisproceduresiprocedure" FOREIGN KEY (procedureid) REFERENCES public.analysisprocedure(procedureid);


--
-- Name: submission submission$employeesubmission; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.submission
    ADD CONSTRAINT "submission$employeesubmission" FOREIGN KEY (technicalofficer) REFERENCES public.employee(employeeid);


--
-- Name: submission submission$mediasubmission; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.submission
    ADD CONSTRAINT "submission$mediasubmission" FOREIGN KEY (mediaid) REFERENCES public.media(mediaid);


--
-- Name: submission submission$tblcustomertblproject1; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.submission
    ADD CONSTRAINT "submission$tblcustomertblproject1" FOREIGN KEY (customerid) REFERENCES public.customer(customerid);


--
-- Name: submission submission$tblprioritytblproject; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.submission
    ADD CONSTRAINT "submission$tblprioritytblproject" FOREIGN KEY (priorityid) REFERENCES public.priority(priorityid);


--
-- Name: templatemetadata templatemetadata_procedureid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.templatemetadata
    ADD CONSTRAINT templatemetadata_procedureid_fkey FOREIGN KEY (procedureid) REFERENCES public.analysisprocedure(procedureid) ON DELETE CASCADE;


--
-- Name: workflowjob workflowjob$analysisprocedureworkflowjob; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.workflowjob
    ADD CONSTRAINT "workflowjob$analysisprocedureworkflowjob" FOREIGN KEY (procedureid) REFERENCES public.analysisprocedure(procedureid);


--
-- Name: workflowjob workflowjob$tblworkflowtblworkflowjobs; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.workflowjob
    ADD CONSTRAINT "workflowjob$tblworkflowtblworkflowjobs" FOREIGN KEY (workflowid) REFERENCES public.workflow(workflowid);


--
-- Name: sianalysiscorrectionfit fk_sianalysiscorrectionfit_sample; Type: FK CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysiscorrectionfit
    ADD CONSTRAINT fk_sianalysiscorrectionfit_sample FOREIGN KEY (sampleid, prefix) REFERENCES public.sample(sampleid, prefix);


--
-- Name: sianalysisinterimdata fk_sianalysisinterimdata_sianalysisdata; Type: FK CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysisinterimdata
    ADD CONSTRAINT fk_sianalysisinterimdata_sianalysisdata FOREIGN KEY (sianalysisdataid) REFERENCES siam.sianalysisrawdata(sianalysisdataid);


--
-- Name: sianalysisrawdata fk_sianalysisrundata_sianalysisrunmetadata; Type: FK CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysisrawdata
    ADD CONSTRAINT fk_sianalysisrundata_sianalysisrunmetadata FOREIGN KEY (sianalysisid, injectionno) REFERENCES siam.sianalysisinjectiondata(sianalysisid, injectionno);


--
-- Name: sianalysisinjectiondata fk_sianalysisrunmetadata_sianalysisrunloadlist; Type: FK CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysisinjectiondata
    ADD CONSTRAINT fk_sianalysisrunmetadata_sianalysisrunloadlist FOREIGN KEY (sianalysisid) REFERENCES siam.sianalysisloadlist(sianalysisid);


--
-- Name: sianalysisresult fk_sianalysisrunresult_sianalysisrunloadlist; Type: FK CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysisresult
    ADD CONSTRAINT fk_sianalysisrunresult_sianalysisrunloadlist FOREIGN KEY (sianalysisid) REFERENCES siam.sianalysisloadlist(sianalysisid);


--
-- Name: siinlets fk_siinlets_simeasurement; Type: FK CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.siinlets
    ADD CONSTRAINT fk_siinlets_simeasurement FOREIGN KEY (siid) REFERENCES siam.simeasurement(siid);


--
-- Name: simeasurement fk_simeasurement_sianalysisrunloadlist; Type: FK CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.simeasurement
    ADD CONSTRAINT fk_simeasurement_sianalysisrunloadlist FOREIGN KEY (sianalysisid) REFERENCES siam.sianalysisloadlist(sianalysisid);


--
-- Name: sianalysiscorrection sianalysiscorrection$tblsianalysisruntblsianalysiscorrections; Type: FK CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysiscorrection
    ADD CONSTRAINT "sianalysiscorrection$tblsianalysisruntblsianalysiscorrections" FOREIGN KEY (sianalysisrunid) REFERENCES siam.sianalysisrun(sianalysisrunid);


--
-- Name: sianalysiscorrectionfitinj sianalysiscorrectiondata$tblsianalysiscorrectionstblsianalysisc; Type: FK CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysiscorrectionfitinj
    ADD CONSTRAINT "sianalysiscorrectiondata$tblsianalysiscorrectionstblsianalysisc" FOREIGN KEY (sianalysisrunid, correctiontype) REFERENCES siam.sianalysiscorrection(sianalysisrunid, correctiontype);


--
-- Name: sianalysisloadlist sianalysisloadlist$tblsampleanalysistblsianalysisloadlist; Type: FK CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysisloadlist
    ADD CONSTRAINT "sianalysisloadlist$tblsampleanalysistblsianalysisloadlist" FOREIGN KEY (analysisid) REFERENCES public.analysis(analysisid);


--
-- Name: sianalysisloadlist sianalysisloadlist$tblsianalysisruntblsianalysisloadlist; Type: FK CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysisloadlist
    ADD CONSTRAINT "sianalysisloadlist$tblsianalysisruntblsianalysisloadlist" FOREIGN KEY (sianalysisrunid) REFERENCES siam.sianalysisrun(sianalysisrunid);


--
-- Name: sianalysisrun sianalysisrun$equipmentsianalysisrun; Type: FK CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysisrun
    ADD CONSTRAINT "sianalysisrun$equipmentsianalysisrun" FOREIGN KEY (equipmentid) REFERENCES public.equipment(equipmentid);


--
-- Name: sianalysisrun sianalysisrun$siproceduresianalysisrun; Type: FK CONSTRAINT; Schema: siam; Owner: postgres
--

ALTER TABLE ONLY siam.sianalysisrun
    ADD CONSTRAINT "sianalysisrun$siproceduresianalysisrun" FOREIGN KEY (procedureid) REFERENCES public.siprocedure(procedureid);


--
-- Name: deuteriumenrichment deuteriumenrichment$electrolysisdeuteriumenrichment; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.deuteriumenrichment
    ADD CONSTRAINT "deuteriumenrichment$electrolysisdeuteriumenrichment" FOREIGN KEY (electrolysisid) REFERENCES trims.electrolysis(electrolysisid);


--
-- Name: electrolysis electrolysis$tblelectrolysisruntblelectrolysis1; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.electrolysis
    ADD CONSTRAINT "electrolysis$tblelectrolysisruntblelectrolysis1" FOREIGN KEY (runid) REFERENCES trims.electrolysisrun(runid);


--
-- Name: electrolysis electrolysis$tblsampleanalysistblelectrolysis1; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.electrolysis
    ADD CONSTRAINT "electrolysis$tblsampleanalysistblelectrolysis1" FOREIGN KEY (analysisid) REFERENCES public.analysis(analysisid);


--
-- Name: electrolysisrun electrolysisrun$electrolysissystemelectrolysisrun; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.electrolysisrun
    ADD CONSTRAINT "electrolysisrun$electrolysissystemelectrolysisrun" FOREIGN KEY (elyssystemid) REFERENCES public.electrolysissystem(elyssystemid);


--
-- Name: electrolysisrun electrolysisrun$enrichmentprocedureelectrolysisrun; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.electrolysisrun
    ADD CONSTRAINT "electrolysisrun$enrichmentprocedureelectrolysisrun" FOREIGN KEY (procedureid) REFERENCES public.enrichmentprocedure(procedureid);


--
-- Name: chemicalenrichment fk_chemenr_analysis; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.chemicalenrichment
    ADD CONSTRAINT fk_chemenr_analysis FOREIGN KEY (analysisid) REFERENCES public.analysis(analysisid);


--
-- Name: chemicalenrichment fk_chemenr_measurables; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.chemicalenrichment
    ADD CONSTRAINT fk_chemenr_measurables FOREIGN KEY (measurableid) REFERENCES public.analytes(analyteid);


--
-- Name: chemicalenrichment fk_chemenr_run; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.chemicalenrichment
    ADD CONSTRAINT fk_chemenr_run FOREIGN KEY (runid) REFERENCES trims.chemenrrun(runid) ON DELETE CASCADE;


--
-- Name: chemenrrun fk_chemenrrun_employee; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.chemenrrun
    ADD CONSTRAINT fk_chemenrrun_employee FOREIGN KEY (technicianid) REFERENCES public.employee(employeeid);


--
-- Name: chemenrrun fk_chemenrrun_measurables; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.chemenrrun
    ADD CONSTRAINT fk_chemenrrun_measurables FOREIGN KEY (measurableid) REFERENCES public.analytes(analyteid);


--
-- Name: lsctrayconfig fk_lsctrayconfig_equipment; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lsctrayconfig
    ADD CONSTRAINT fk_lsctrayconfig_equipment FOREIGN KEY (equipmentid) REFERENCES public.equipment(equipmentid);


--
-- Name: guitblimportmapping fk_mapping_format; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.guitblimportmapping
    ADD CONSTRAINT fk_mapping_format FOREIGN KEY (formatid) REFERENCES public.guitblfileformat(lngformatid);


--
-- Name: lscprotocolmapping fk_mapping_protocol; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscprotocolmapping
    ADD CONSTRAINT fk_mapping_protocol FOREIGN KEY (protocolid) REFERENCES trims.lscprotocol(protocolid);


--
-- Name: lscprocedureprotocol fk_pp_protocol; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscprocedureprotocol
    ADD CONSTRAINT fk_pp_protocol FOREIGN KEY (protocolid) REFERENCES trims.lscprotocol(protocolid);


--
-- Name: lscrunprotocol fk_runprotocol_protocol; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscrunprotocol
    ADD CONSTRAINT fk_runprotocol_protocol FOREIGN KEY (protocolid) REFERENCES trims.lscprotocol(protocolid);


--
-- Name: lscrunprotocol fk_runprotocol_run; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscrunprotocol
    ADD CONSTRAINT fk_runprotocol_run FOREIGN KEY (runid) REFERENCES trims.lscrun(runid);


--
-- Name: lscprotocolsettings fk_settings_protocol; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscprotocolsettings
    ADD CONSTRAINT fk_settings_protocol FOREIGN KEY (protocolid) REFERENCES trims.lscprotocol(protocolid);


--
-- Name: lscloadlist lscloadlist$tblcounterruntblcounterloadlist1; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscloadlist
    ADD CONSTRAINT "lscloadlist$tblcounterruntblcounterloadlist1" FOREIGN KEY (runid) REFERENCES trims.lscrun(runid);


--
-- Name: lscloadlist lscloadlist$tblsampleanalysistblcounterloadlist1; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscloadlist
    ADD CONSTRAINT "lscloadlist$tblsampleanalysistblcounterloadlist1" FOREIGN KEY (analysisid) REFERENCES public.analysis(analysisid);


--
-- Name: lscresult lscresult$tblcounterruntblresult1; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscresult
    ADD CONSTRAINT "lscresult$tblcounterruntblresult1" FOREIGN KEY (runid) REFERENCES trims.lscrun(runid);


--
-- Name: lscresult lscresult$tblsampleanalysistblresult1; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscresult
    ADD CONSTRAINT "lscresult$tblsampleanalysistblresult1" FOREIGN KEY (analysisid) REFERENCES public.analysis(analysisid);


--
-- Name: lscrun lscrun$lscprocedurelscrun; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscrun
    ADD CONSTRAINT "lscrun$lscprocedurelscrun" FOREIGN KEY (procedureid) REFERENCES public.lscprocedure(procedureid);


--
-- Name: lscrunmean lscrunmean$tblcounterloadlisttblcounterrunmean1; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscrunmean
    ADD CONSTRAINT "lscrunmean$tblcounterloadlisttblcounterrunmean1" FOREIGN KEY (countid) REFERENCES trims.lscloadlist(countid);


--
-- Name: lscrunraw lscrunraw$tblcounterloadlisttblcounterrunraw1; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.lscrunraw
    ADD CONSTRAINT "lscrunraw$tblcounterloadlisttblcounterrunraw1" FOREIGN KEY (countid) REFERENCES trims.lscloadlist(countid);


--
-- Name: primarydistillation primarydistillation$tblprimarydistillationbatchtblprimarydistil; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.primarydistillation
    ADD CONSTRAINT "primarydistillation$tblprimarydistillationbatchtblprimarydistil" FOREIGN KEY (runid) REFERENCES trims.primarydistillationbatch(runid);


--
-- Name: primarydistillation primarydistillation$tblsampleanalysistblprimarydistillation1; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.primarydistillation
    ADD CONSTRAINT "primarydistillation$tblsampleanalysistblprimarydistillation1" FOREIGN KEY (analysisid) REFERENCES public.analysis(analysisid);


--
-- Name: primarydistillationbatch primarydistillationbatch$distillationprocedureprimarydistillati; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.primarydistillationbatch
    ADD CONSTRAINT "primarydistillationbatch$distillationprocedureprimarydistillati" FOREIGN KEY (procedureid) REFERENCES public.analysisprocedure(procedureid);


--
-- Name: primarydistillationbatch primarydistillationbatch$equipmentprimarydistillationbatch; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.primarydistillationbatch
    ADD CONSTRAINT "primarydistillationbatch$equipmentprimarydistillationbatch" FOREIGN KEY (equipmentid) REFERENCES public.equipment(equipmentid);


--
-- Name: primarydistillationdata primarydistillationdata$tblprimarydistillationtblprimarydistill; Type: FK CONSTRAINT; Schema: trims; Owner: postgres
--

ALTER TABLE ONLY trims.primarydistillationdata
    ADD CONSTRAINT "primarydistillationdata$tblprimarydistillationtblprimarydistill" FOREIGN KEY (id) REFERENCES trims.primarydistillation(id);


--
-- PostgreSQL database dump complete
--

\unrestrict aMM6NP5T7tr7JxTu9hSS1Ck8IRSNUHwAh2NrnuhtUYSaQAFGyAT4TpaSqG5oIG8

