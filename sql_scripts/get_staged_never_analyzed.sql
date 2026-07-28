-- Script to view samples that are staged but have never been analyzed
SELECT 
    s.sampleid,
    s.prefix || '-' || s.sampleid AS lab_id,
    s.status ,
    s.sname AS sample_name,
    s.submissionid,
    sub.submissionname AS project_name,
    s.workflowid AS sample_workflow_id,
    sub.requestedworkflow AS project_workflow_id,
    s.collectiondate AS sampling_date,
    sub.submissiondate AS project_submission_date,
    s.createdatestamp AS sample_created_date,
    q.queued_at,
    q.queued_by 
FROM 
    public.sample s
inner join
	sample_queue q on q.prefix = s.prefix and q.sampleid =s.sampleid 
LEFT JOIN 
    public.submission sub ON s.submissionid = sub.submissionid
WHERE sub.submissiontype <> 1
and NOT EXISTS (
        SELECT 1 
        FROM public.analysis a 
        WHERE a.sampleid = s.sampleid and a.prefix =s.prefix 
    )
ORDER BY 
    s.submissionid ASC, 
    s.sampleid ASC;
