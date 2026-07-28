-- =============================================================================
-- usp_GetLscRunDetails  — RS1: run header  |  RS2: standards & spikes
-- Called by frmCountRun_2 via ADO (two result sets only).
-- =============================================================================
CREATE OR ALTER PROCEDURE usp_GetLscRunDetails
    @RunID INT
AS
BEGIN
    SET NOCOUNT ON;

    -- RS1: Run header
    SELECT
        r.RunID,
        r.RunStartTime,
        r.RunEndTime,
        r.CounterEfficiency,
        r.CounterEfficiencyUnc,
        r.MeanBackground,
        r.MeanBackgroundUnc,
        r.MeanStandard,
        r.MeanStandardUnc
    FROM TRIMS.LSCRun r
    WHERE r.RunID = @RunID;

    -- RS2: Standards & Spikes — certified values only
    -- Decay correction is applied client-side in GetStandardActivity if needed;
    -- for the weighted-mean display in the header this raw value is sufficient.
    SELECT
        s.SampleType,
        s.SampleID,
        rcd.CertifiedValue,
        rcd.CertifiedValueUnc
    FROM       TRIMS.LSCLoadList    ll
    INNER JOIN Analysis              a   ON  a.AnalysisID  = ll.AnalysisID
    INNER JOIN Sample                s   ON  s.SampleID    = a.SampleID
                                        AND  s.Prefix      = a.Prefix
    INNER JOIN ReferenceControl      rc  ON  rc.SampleID   = s.SampleID
                                        AND  rc.Prefix     = s.Prefix
    INNER JOIN ReferenceControlData  rcd ON  rcd.ReferenceID = rc.ReferenceID
    WHERE ll.RunID = @RunID
      AND s.SampleType IN (2, 3);   -- 2=Standard  3=Spike
END
GO


-- =============================================================================
-- usp_GetLscRunGrid  — single result set for sfrmCountSamples subform.
-- Replaces GUItblWaterMasses via inline temp table (buoyancy formula).
-- @Filter:  SQL fragment injected into WHERE, e.g.
--           'AND ll.SampleType NOT IN (8,9)'   (default)
--           'AND ll.SampleType = 0'            (samples only)
-- =============================================================================
CREATE OR ALTER PROCEDURE usp_GetLscRunGrid
    @RunID  INT,
    @Filter NVARCHAR(200) = 'AND ll.SampleType NOT IN (8,9)'
AS
BEGIN
    SET NOCOUNT ON;

    -- Buoyancy constants (fall back to physical defaults if not configured)
    DECLARE @gWaterDensity FLOAT = 997.05,
            @gRefDensity   FLOAT = 8000.0,
            @gAirDensity   FLOAT = 1.2;

    SELECT @gWaterDensity = TRY_CAST(TokenValue AS FLOAT) FROM GlobalValue WHERE Token = 'gWaterDensity';
    SELECT @gRefDensity   = TRY_CAST(TokenValue AS FLOAT) FROM GlobalValue WHERE Token = 'gRefDensity';
    SELECT @gAirDensity   = TRY_CAST(TokenValue AS FLOAT) FROM GlobalValue WHERE Token = 'gAirDensity';

    DECLARE @BC FLOAT =
        CASE WHEN (1.0 - @gAirDensity / @gWaterDensity) <> 0
             THEN (1.0 - @gAirDensity / @gRefDensity) / (1.0 - @gAirDensity / @gWaterDensity)
             ELSE 1.0 END;

    -- Compute water masses inline (replaces GUItblWaterMasses VBA pre-step)
    CREATE TABLE #WM (
        ElectrolysisID INT, AnalysisID INT,
        InitialWater   FLOAT, InitialWaterUnc FLOAT,
        FinalWater     FLOAT, FinalWaterUnc   FLOAT
    );

    INSERT INTO #WM
    SELECT
        e.ElectrolysisID,
        e.AnalysisID,
        -- InitialWater = (FullCellBefore - EmptyCell - 0.715×Na2O2) × BC
        -- (0.715 = 0.59 + 0.205 - 0.08, Taylor 1977)
        (ISNULL(e.FullCellMassBefore,0) - ISNULL(e.MassEmptyCell,0)
         - 0.715 * ISNULL(e.Na2O2Mass,0)) * @BC,
        0.0,
        CASE
            WHEN ISNULL(e.FinalCellMassAfter,0) = 0 AND e.ColdTrapMassFilled IS NOT NULL
            THEN (e.ColdTrapMassFilled - e.ColdTrapMassBefore)
            ELSE (ISNULL(e.FinalCellMassAfter,0) - ISNULL(e.MassEmptyCell,0)
                  - 0.59 * ISNULL(e.Na2O2Mass,0))
        END * @BC,
        0.0
    FROM TRIMS.Electrolysis e
    WHERE e.AnalysisID IN (SELECT AnalysisID FROM TRIMS.LSCLoadList WHERE RunID = @RunID);

    -- Dynamic filter — safe: @Filter is constructed entirely in VBA, not from user input
    DECLARE @sql NVARCHAR(MAX);
    SET @sql = N'
    SELECT
        ll.RunID                                AS LSCRun,
        s.SampleID,
        ll.AnalysisID,
        s.sName                                 AS SampleName,
        ll.PositionInRun                        AS Position,
        ll.TrayNumber,
        ll.PositionInTray,
        ll.SampleAmount,
        ll.SampleDiluent,
        ll.SampleType,
        ll.IsLocked,
        ll.Remarks,
        r.MinutesCompleted                      AS CountTime,
        r.MeanBackground,
        r.MeanBackgroundUnc,
        r.MeanStandard,
        r.MeanStandardUnc,
        r.CounterEfficiency                     AS Efficiency,
        wm.InitialWater,
        wm.InitialWaterUnc,
        wm.FinalWater,
        wm.FinalWaterUnc,
        e.EnrichmentFactor                      AS ElysEnrFactor,
        res.EnrichmentFactorUnc                 AS ElysEnrFactorUnc,
        res.LLDstatus,
        res.EnrichmentFactor,
        res.EnrichmentFactorMethod,
        ll.Result,
        ll.ResultUnc,
        cpm.MeanValue                           AS MeanCPM,
        cpm.MeanValueUnc                        AS MeanCPMunc,
        res.FinalActivity,
        res.FinalActivityUnc,
        res.ActivityUnit,
        mu.ShortName                            AS ActivityUnitName,
        meth.sName                              AS EnrMethod,
        de.EnrichmentFactor                     AS [2H_EnrFactor],
        de.EnrichmentFactorUnc                  AS [2H_EnrFactorUnc],
        de.DeuteriumRecovery                    AS [2H_Recovery]
    FROM       TRIMS.LSCLoadList                 ll
    INNER JOIN TRIMS.LSCRun                      r    ON  r.RunID       = ll.RunID
    INNER JOIN Analysis                          a    ON  a.AnalysisID  = ll.AnalysisID
    INNER JOIN Sample                            s    ON  s.SampleID    = a.SampleID
                                                     AND s.Prefix      = a.Prefix
    LEFT  JOIN (SELECT CountID, MeanValue, MeanValueUnc
                FROM   TRIMS.LSCRunMean WHERE ValueKind = 1)  cpm
                                                      ON  cpm.CountID   = ll.CountID
    LEFT  JOIN TRIMS.LSCResult                   res  ON  res.AnalysisID = ll.AnalysisID
                                                     AND  res.RunID      = ll.RunID
    LEFT  JOIN MeasurementUnit                   mu   ON  mu.UnitID      = res.ActivityUnit
    LEFT  JOIN GuitblEnrichmentFactorMethod      meth ON  meth.ID        = res.EnrichmentFactorMethod
    LEFT  JOIN TRIMS.Electrolysis                e    ON  e.AnalysisID   = ll.AnalysisID
    LEFT  JOIN #WM                               wm   ON  wm.ElectrolysisID = e.ElectrolysisID
    LEFT  JOIN TRIMS.DeuteriumEnrichment         de   ON  de.ElectrolysisID = e.ElectrolysisID
    WHERE  ll.RunID = ' + CAST(@RunID AS NVARCHAR(20)) + '
      AND  s.Prefix = ''J''
      ' + @Filter + '
    ORDER BY ll.PositionInRun;';

    EXEC sp_executesql @sql;

    DROP TABLE #WM;
END
GO
