from __future__ import annotations

import math
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Callable
from sqlalchemy import text
from db_core import db_manager

from ngam_protocol_processor import (
    ReproReferenceInfo, MultiRunLinearityData, SequenceProcessingResult,
    IsotopeResult, BlankFit, DriftFit, LinearityFit,
)
from ngam_protocol_parser import ProtocolSequence

log = logging.getLogger(__name__)

_LV_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)

def _lv_to_dt(lv_ts: Optional[float]) -> Optional[datetime]:
    """Convert a LabVIEW timestamp (seconds since 1904-01-01 UTC) to a UTC datetime."""
    if lv_ts is None:
        return None
    return _LV_EPOCH + timedelta(seconds=lv_ts)

# Maps referencecontroldata measurable name to processor isotope key.
MEASURABLE_TO_ISO_KEY = {
    # ── Concentrations (bisratio=FALSE) ──────────────────────────────────────
    "3he concentration":      "SMS:3He",
    "he-3":                   "SMS:3He",
    "he3":                    "SMS:3He",
    "4he concentration":      "SMS:4He",
    "he-4":                   "SMS:4He",
    "he4":                    "SMS:4He",
    "20ne concentration":     "QMSNe:20Ne",
    "ne-20":                  "QMSNe:20Ne",
    "ne20":                   "QMSNe:20Ne",
    "21ne concentration":     "QMSNe:21Ne",
    "ne21":                   "QMSNe:21Ne",
    "22ne concentration":     "QMSNe:22Ne",
    "ne22":                   "QMSNe:22Ne",
    "36ar concentration":     "QMSAr:36Ar",
    "ar36":                   "QMSAr:36Ar",
    "38ar concentration":     "QMSAr:38Ar",
    "ar38":                   "QMSAr:38Ar",
    "40ar concentration":     "QMSAr:40Ar",
    "ar40":                   "QMSAr:40Ar",
    "82kr concentration":     "QMSKrXe:82Kr",
    "83kr concentration":     "QMSKrXe:83Kr",
    "84kr concentration":     "QMSKrXe:84Kr",
    "86kr concentration":     "QMSKrXe:86Kr",
    "129xe concentration":    "QMSKrXe:129Xe",
    "131xe concentration":    "QMSKrXe:131Xe",
    "132xe concentration":    "QMSKrXe:132Xe",
    "134xe concentration":    "QMSKrXe:134Xe",
    "136xe concentration":    "QMSKrXe:136Xe",
    # ── Ratios (bisratio=TRUE) ── keys come from public.analytes.analytename ─
    # DB stores analyte names without slash; map directly to slash keys used by
    # NG_RATIOS so certified_values["3He/4He"] is set for the repro-ref path.
    "3he4he":                 "3He/4He",
    "20ne22ne":               "20Ne/22Ne",
    "21ne22ne":               "21Ne/22Ne",
    "38ar36ar":               "38Ar/36Ar",
    "40ar36ar":               "40Ar/36Ar",
    "82kr84kr":               "82Kr/84Kr",
    "83kr84kr":               "83Kr/84Kr",
    "86kr84kr":               "86Kr/84Kr",
    "129xe132xe":             "129Xe/132Xe",
    "131xe132xe":             "131Xe/132Xe",
    "134xe132xe":             "134Xe/132Xe",
    "136xe132xe":             "136Xe/132Xe",
}


def _find_referenceid(conn, pfx: str, sid_int: int, run_date: Optional[datetime]) -> Optional[int]:
    """Return the most recent referenceid for prefix+sampleid valid on run_date.

    If run_date is None, date filtering is skipped (backward-compatible fallback).
    Returns None when no matching calibration is found.
    """
    if run_date is not None:
        row = conn.execute(text("""
            SELECT referenceid FROM public.referencecontrol
            WHERE prefix = :pfx AND sampleid = :sid
              AND (availabledatefrom IS NULL OR availabledatefrom <= :rd)
              AND (availabledateto IS NULL OR availabledateto >= :rd)
            ORDER BY referenceid DESC LIMIT 1
        """), {"pfx": pfx, "sid": sid_int, "rd": run_date}).fetchone()
    else:
        row = conn.execute(text("""
            SELECT referenceid FROM public.referencecontrol
            WHERE prefix = :pfx AND sampleid = :sid
            ORDER BY referenceid DESC LIMIT 1
        """), {"pfx": pfx, "sid": sid_int}).fetchone()
    return row[0] if row else None


def build_repro_references(
    target_run_id: int,
    run_date: Optional[datetime] = None,
) -> Tuple[Optional[List[ReproReferenceInfo]], List[str]]:
    """Query ngpreparations for bisreproreference inlets and resolve their
    certified values from referencecontroldata.

    Returns (references, missing_labids).  missing_labids is non-empty when a
    labid had no valid referencecontrol row covering run_date.
    """
    try:
        with db_manager.get_connection() as conn:
            preps = conn.execute(text("""
                SELECT p.positioninrun, p.nvcinletstring ourlabid, r.runstarttime
                FROM ngam.ngpreparations p
                JOIN ngam.ngsequencerun r
                    ON r.runid = p.runid
                WHERE p.runid = :sid
                  AND p.bisreproreference = TRUE
            """), {"sid": target_run_id}).fetchall()
    except Exception as e:
        log.error(f"Repro reference query failed: {e}")
        return None, []

    if not preps:
        return None, []

    result: List[ReproReferenceInfo] = []
    missing: List[str] = []

    for row in preps:
        seq_num = int(row[0])
        labid = (row[1] or "").strip()
        run_date = row[2]
        if not labid or "-" not in labid:
            continue
        pfx, sid_str = labid.split("-", 1)
        run_date = row[2]
        try:
            sid_int = int(sid_str)
        except ValueError:
            continue

        try:
            with db_manager.get_connection() as conn:
                rid = _find_referenceid(conn, pfx, sid_int, run_date)
                if rid is None:
                    if run_date is not None:
                        missing.append(labid)
                    continue

                certs = conn.execute(text("""
                    SELECT a.analytename,
                           rcd.certifiedvalue,
                           rcd.certifiedvalueunc
                    FROM public.referencecontroldata rcd
                    JOIN public.analytes a ON a.analyteid = rcd.measurableid
                    WHERE rcd.referenceid = :rid
                      AND rcd.certifiedvalue IS NOT NULL
                """), {"rid": rid}).fetchall()
        except Exception as e:
            log.error(f"Certified value query failed for {labid}: {e}")
            continue

        if not certs:
            continue

        certified_values: Dict[str, Tuple[float, float]] = {}
        for c_row in certs:
            mname = (c_row[0] or "").strip().lower()
            iso_key = MEASURABLE_TO_ISO_KEY.get(mname)
            if iso_key and c_row[1] is not None:
                certified_values[iso_key] = (float(c_row[1]), float(c_row[2] or 0.0))

        if certified_values:
            result.append(ReproReferenceInfo(
                seq_num=seq_num,
                ourlabid=labid,
                certified_values=certified_values,
            ))

    return (result if result else None), missing


def build_aliquot_volumes(target_run_id: int) -> Optional[Dict[int, float]]:
    """Read aliquot volumes (freferenceamount) from ngpreparations."""
    try:
        with db_manager.get_connection() as conn:
            rows = conn.execute(text("""
                SELECT positioninrun, freferenceamount
                FROM ngam.ngpreparations
                WHERE runid = :sid
                  AND freferenceamount IS NOT NULL
            """), {"sid": target_run_id}).fetchall()
    except Exception as e:
        log.error(f"Aliquot volume query failed: {e}")
        return None

    if not rows:
        return None
    return {int(r[0]): float(r[1]) for r in rows if r[1] and float(r[1]) > 0}


def build_db_inlet_roles(target_run_id: int) -> Dict[int, str]:
    """Return {seq_num: role} for all non-sample inlets in a sequence run.

    Role values: 'blank', 'repro_ref', 'lin_ref', 'repro_lin_ref'.
    'repro_lin_ref' is used when bisreproreference AND bislinreference are both True
    so the inlet participates in BOTH the drift fit and the linearity fit.
    Inlets not matching any flag are omitted (let the protocol classifier decide).
    Precedence when blank is set alongside others: blank > everything.
    """
    try:
        with db_manager.get_connection() as conn:
            rows = conn.execute(text("""
                SELECT positioninrun,
                       COALESCE(bisblank,           FALSE) AS is_blank,
                       COALESCE(bisreproreference,  FALSE) AS is_repro,
                       COALESCE(bislinreference,    FALSE) AS is_lin
                FROM ngam.ngpreparations
                WHERE runid = :sid
                  AND (COALESCE(bisblank, FALSE)
                       OR COALESCE(bisreproreference, FALSE)
                       OR COALESCE(bislinreference, FALSE))
            """), {"sid": target_run_id}).fetchall()
    except Exception as e:
        log.error(f"DB inlet role query failed: {e}")
        return {}

    result: Dict[int, str] = {}
    for row in rows:
        seq_num  = int(row[0])
        is_blank = bool(row[1])
        is_repro = bool(row[2])
        is_lin   = bool(row[3])
        if is_blank:
            result[seq_num] = "blank"
        elif is_repro and is_lin:
            result[seq_num] = "repro_lin_ref"
        elif is_repro:
            result[seq_num] = "repro_ref"
        elif is_lin:
            result[seq_num] = "lin_ref"
    return result


def build_extraction_info(target_run_id: int):
    """Query ngextractiondata for water samples linked to a sequence run.

    Returns {positioninrun: ExtractionInfo} or None if no records exist.

    Per-element efficiency (η) is resolved in two stages:
      1. ngextractionlineefficiency — most recent valid record for the
         extraction run's equipment at the sample time (one per element).
      2. ngextractiondata.extraction_efficiency — scalar per-sample fallback.
    """
    try:
        with db_manager.get_connection() as conn:
            rows = conn.execute(text("""
                SELECT
                    p.positioninrun,
                    CASE COALESCE(s.container_type, 1)
                        WHEN 1 THEN (ed.fweightwaterbulbafter - ed.fweightwaterbulbbefore)
                        WHEN 2 THEN (ed.fweighttubebefore     - ed.fweighttubeafter)
                        WHEN 3 THEN ed.sample_volume_ml
                        ELSE NULL
                    END                            AS water_mass_g,
                    ed.temperature_c,
                    ed.salinity_ppt,
                    ed.altitude_m,
                    ed.extraction_efficiency,
                    er.equipmentid,
                    COALESCE(ed.dtimestart, er.runstarttime, now()),
                    ed.lab_pressure_torr
                FROM ngam.ngpreparations p
                JOIN ngam.ngextractiondata ed
                    ON  ed.extractionid = p.extractionid        -- direct FK (post-036)
                    OR  (p.extractionid IS NULL                  -- fallback for pre-036 rows
                         AND ed.analysisid = p.analysisid)
                JOIN ngam.ngextractionrun  er ON er.runid       = ed.runid
                JOIN public.analysis        a ON a.analysisid   = ed.analysisid
                JOIN public.sample          s ON s.sampleid     = a.sampleid
                                             AND s.prefix       = a.prefix
                WHERE p.runid = :sid
                  AND ed.isignored IS DISTINCT FROM 1
                  AND CASE COALESCE(s.container_type, 1)
                        WHEN 1 THEN (ed.fweightwaterbulbafter - ed.fweightwaterbulbbefore)
                        WHEN 2 THEN (ed.fweighttubebefore     - ed.fweighttubeafter)
                        WHEN 3 THEN ed.sample_volume_ml
                        ELSE NULL
                      END > 0
                ORDER BY p.positioninrun
            """), {"sid": target_run_id}).fetchall()
    except Exception as e:
        log.error(f"Extraction info query failed: {e}")
        return None

    if not rows:
        return None

    from ngam_protocol_processor import ExtractionInfo

    # Collect unique (equipment_id, sample_time) pairs so we can do one
    # efficiency lookup per unique combination.
    combos: dict = {}   # (equipment_id, sample_time) → {element: (η, ±η)}
    for row in rows:
        equipment_id = row[6]
        sample_time  = row[7]
        key = (equipment_id, sample_time)
        if key not in combos:
            combos[key] = {}

    # Query element-specific efficiency for each unique combo.
    # Wrapped in try/except so a missing table (migration not yet applied)
    # degrades gracefully — the scalar fallback still works.
    for (equipment_id, sample_time) in combos:
        try:
            with db_manager.get_connection() as conn:
                eff_rows = conn.execute(text("""
                    SELECT DISTINCT ON (element)
                        element, efficiency, efficiency_unc
                    FROM ngam.ngextractionlineefficiency
                    WHERE (equipmentid = :eq OR equipmentid IS NULL)
                      AND valid_from <= :t
                      AND (valid_until IS NULL OR valid_until > :t)
                    ORDER BY element, equipmentid NULLS LAST, valid_from DESC
                """), {"eq": equipment_id, "t": sample_time}).fetchall()
            combos[(equipment_id, sample_time)] = {
                str(r[0]).strip(): (
                    float(r[1]),
                    float(r[2]) if r[2] is not None else None,
                )
                for r in eff_rows
            }
        except Exception as e:
            log.debug(f"Line efficiency lookup skipped (table may not exist): {e}")

    result = {}
    for row in rows:
        seq_num      = int(row[0])
        water_mass_g = float(row[1])
        equipment_id = row[6]
        sample_time  = row[7]
        lab_press    = row[8]   # lab_pressure_torr (may be None)
        elem_eff     = combos.get((equipment_id, sample_time), {})

        # When lab_pressure_torr is set (EQW samples), convert to an equivalent
        # altitude so process_sequence's pressure_from_altitude() gives the right
        # p_atm = lab_pressure_torr / 760.  Formula: alt = -8500 * ln(p_torr/760).
        if lab_press is not None and float(lab_press) > 0:
            import math as _math
            altitude_m = -8500.0 * _math.log(float(lab_press) / 760.0)
        else:
            altitude_m = float(row[4]) if row[4] is not None else None

        result[seq_num] = ExtractionInfo(
            seq_num=seq_num,
            water_mass_g=water_mass_g,
            temperature_c=float(row[2]) if row[2] is not None else None,
            salinity_ppt=float(row[3]) if row[3] is not None else 0.0,
            altitude_m=altitude_m,
            extraction_efficiency=float(row[5]) if row[5] is not None else None,
            element_efficiency={el: v[0] for el, v in elem_eff.items()},
            element_efficiency_unc={
                el: v[1] for el, v in elem_eff.items() if v[1] is not None
            },
        )
    return result if result else None


def build_dilution_info(target_run_id: int) -> Dict[str, float]:
    """Query ngdilutionfactor for the dilution factors of Helium (He) and Neon (Ne)
    applicable to this sequence run based on its equipmentid and start time.
    Returns:
        Dict[str, float]: {"He": he_factor, "Ne": ne_factor}
    """
    from datetime import datetime, timezone
    result = {"He": 2.4, "Ne": 2.4}
    try:
        with db_manager.get_connection() as conn:
            # 1. Fetch sequence run equipment and date
            run_row = conn.execute(text("""
                SELECT equipmentid, createdatestamp
                FROM ngam.ngsequencerun
                WHERE runid = :sid
            """), {"sid": target_run_id}).fetchone()
            
            if not run_row:
                return result
                
            equipment_id = run_row[0]
            run_time = run_row[1] if run_row[1] is not None else datetime.now(timezone.utc)
            
            # 2. Query dilution factors for He and Ne
            for element in ["He", "Ne"]:
                factor_row = conn.execute(text("""
                    SELECT dilution_factor
                    FROM ngam.ngdilutionfactor
                    WHERE (equipmentid = :eq OR equipmentid IS NULL)
                      AND valid_from <= :t
                      AND (valid_until IS NULL OR valid_until > :t)
                      AND element = :elem
                    ORDER BY equipmentid NULLS LAST, valid_from DESC
                    LIMIT 1
                """), {"eq": equipment_id, "t": run_time, "elem": element}).fetchone()
                if factor_row:
                    result[element] = float(factor_row[0])
    except Exception as e:
        log.warning(f"Failed to query dilution factors from DB (using default 2.4): {e}")
    return result


def build_spike_certified_values(
    idms_config: dict,
    run_date: Optional[datetime] = None,
) -> Tuple[Optional[Dict[str, Tuple[float, float, float, float]]], List[str]]:
    """Resolve certified spike amounts and isotope ratios from ngreference.

    All spike inlets share the same Spike gas CVs (from ngam.ngreference
    keyed by nvcgasname='Spike').  Looks up via ngam_reference_lookup so
    that date-range filtering and fallback warnings are handled consistently.

    Returns
    -------
    (data, warnings)
        data     – ``{labid: (A_spike, A_spike_unc, R_spike, R_spike_unc)}``
        warnings – human-readable strings (empty when CVs matched exactly)
    """
    if not idms_config:
        return None, []

    # Collect all unique (spike_labid, target_isotope, spike_isotope) combos
    combos: set = set()
    for target_iso, entry in idms_config.items():
        spike_iso = entry.get("spike_isotope", "")
        spike_per_inlet = entry.get("spike_per_inlet", {})
        if not spike_iso or not spike_per_inlet:
            continue
        for _seq_num, labid in spike_per_inlet.items():
            combos.add((str(labid), str(target_iso), str(spike_iso)))

    if not combos:
        return None, []

    # Load CVs once for all spike inlets (they all share the same Spike reference)
    from ngam_reference_lookup import load_amounts_with_unc_by_gastype
    rd = run_date.date() if run_date is not None else None
    try:
        with db_manager.get_connection() as conn:
            cv_values, cv_warning = load_amounts_with_unc_by_gastype("Spike", conn, rd)
    except Exception as e:
        log.error("Failed to load Spike CVs from ngreference: %s", e)
        cv_values, cv_warning = {}, None

    warnings: List[str] = [cv_warning] if cv_warning else []

    if not cv_values:
        log.warning("No Spike certified values found (run_date=%s)", rd)
        return None, warnings

    def _get_cv(iso: str):
        """Look up (value, unc) by bare species name."""
        if iso in cv_values:
            return cv_values[iso]
        for k, v in cv_values.items():
            if (k.split(":", 1)[1] if ":" in k else k) == iso:
                return v
        return None

    result: dict = {}
    for labid, target_iso, spike_iso in combos:
        spike_val = _get_cv(spike_iso)
        target_val = _get_cv(target_iso)
        if spike_val is None or target_val is None:
            log.warning("Spike CV missing for spike_iso=%r or target_iso=%r", spike_iso, target_iso)
            continue
        A_spike, A_spike_unc = spike_val
        B_spike, B_spike_unc = target_val
        if B_spike <= 0:
            continue
        R_spike = A_spike / B_spike
        rel_a = A_spike_unc / A_spike if A_spike != 0 else 0.0
        rel_b = B_spike_unc / B_spike if B_spike != 0 else 0.0
        R_spike_unc = R_spike * math.sqrt(rel_a ** 2 + rel_b ** 2)
        result[labid] = (A_spike, A_spike_unc, R_spike, R_spike_unc)

    return (result if result else None), warnings


def build_multi_run_linearity(target_run_id: int) -> Optional[List[MultiRunLinearityData]]:
    """Query nglinearitysnapshots for historical linearity data."""
    try:
        with db_manager.get_connection() as conn:
            # Find this run's linearity reference ourlabid
            lin_ref = conn.execute(text("""
                SELECT p.positioninrun, t.ourlabid
                FROM ngam.ngpreparations p
                JOIN ngam.ngsequencerun r
                    ON r.runid = p.runid
                LEFT JOIN public.ngseqtemplate t
                    ON t.procedureid = r.procedureid
                    AND t.iinletid = p.positioninrun
                WHERE p.runid = :sid
                  AND p.bislinreference = TRUE
                LIMIT 1
            """), {"sid": target_run_id}).fetchone()
    except Exception as e:
        log.error(f"Linearity reference lookup failed: {e}")
        return None

    if not lin_ref:
        return None
    lin_labid = (lin_ref[1] or "").strip()
    if not lin_labid:
        return None

    try:
        with db_manager.get_connection() as conn:
            rows = conn.execute(text("""
                SELECT isotope_key, signal_level, sensitivity, run_id
                FROM ngam.nglinearitysnapshots
                WHERE ourlabid = :labid
                ORDER BY run_id, isotope_key
            """), {"labid": lin_labid}).fetchall()
    except Exception as e:
        log.error(f"Multi-run linearity query failed: {e}")
        return None

    if not rows:
        return None

    grouped: Dict[str, MultiRunLinearityData] = {}
    for r in rows:
        iso_key = r[0]
        if iso_key not in grouped:
            grouped[iso_key] = MultiRunLinearityData(isotope_key=iso_key)
        grouped[iso_key].signal_levels.append(float(r[1]))
        grouped[iso_key].sensitivities.append(float(r[2]))
        grouped[iso_key].run_ids.append(int(r[3]))

    return list(grouped.values()) if grouped else None


def save_linearity_snapshots(result: SequenceProcessingResult, target_run_id: int) -> None:
    """Persist per-standard sensitivity/signal for future multi-run linearity."""
    if not target_run_id:
        return

    # Find the linearity reference in this run's preparations
    try:
        with db_manager.get_connection() as conn:
            lin_refs = conn.execute(text("""
                SELECT p.positioninrun, t.ourlabid
                FROM ngam.ngpreparations p
                JOIN ngam.ngsequencerun r
                    ON r.runid = p.runid
                LEFT JOIN public.ngseqtemplate t
                    ON t.procedureid = r.procedureid
                    AND t.iinletid = p.positioninrun
                WHERE p.runid = :sid
                  AND p.bislinreference = TRUE
            """), {"sid": target_run_id}).fetchall()
    except Exception as e:
        log.error(f"Save linearity snapshots query failed: {e}")
        return

    seq_nums_to_labid: Dict[int, str] = {}
    for r in lin_refs:
        labid = (r[1] or "").strip()
        if labid:
            seq_nums_to_labid[int(r[0])] = labid

    if not seq_nums_to_labid:
        return

    rows_to_insert: list = []
    for ir in result.inlets:
        if ir.seq_num not in seq_nums_to_labid:
            continue
        labid = seq_nums_to_labid[ir.seq_num]
        for key, iso in ir.isotopes.items():
            if math.isnan(iso.inlet_sensitivity) or math.isnan(iso.blank_corrected):
                continue
            rows_to_insert.append({
                "ourlabid": labid,
                "iso_key": key,
                "sig": iso.blank_corrected,
                "sens": iso.inlet_sensitivity,
            })

    if not rows_to_insert:
        return

    try:
        with db_manager.get_connection() as conn:
            for row in rows_to_insert:
                conn.execute(text("""
                    INSERT INTO ngam.nglinearitysnapshots
                        (ourlabid, isotope_key, signal_level, sensitivity, run_id)
                    VALUES (:labid, :key, :sig, :sens, :rid)
                """), {
                    "labid": row["ourlabid"],
                    "key":   row["iso_key"],
                    "sig":   row["sig"],
                    "sens":  row["sens"],
                    "rid":   target_run_id,
                })
            conn.commit()
        log.info(f"Saved {len(rows_to_insert)} linearity snapshot(s) for run {target_run_id}")
    except Exception as e:
        log.error(f"Save linearity snapshots failed: {e}")


def get_existing_sequence_id(protocol_path: str, lv_time_start: float) -> Optional[int]:
    """Check if the sequence already exists in the database by protocol path and start time."""
    try:
        with db_manager.get_connection() as conn:
            existing = conn.execute(text("""
                SELECT runid FROM ngam.ngsequencerun
                WHERE nvcprotocolfilepath=:path
                  AND runstarttime = :ts
            """), {"path": protocol_path, "ts": _lv_to_dt(lv_time_start)}).fetchone()
            return existing[0] if existing is not None else None
    except Exception as e:
        log.error(f"Failed to check existing sequence: {e}")
        return None


def import_sequence(
    seq: ProtocolSequence,
    run_id: Optional[int],
    delete_existing: bool = False,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    user_stamp: str = "unknown",
    measurement_mode: str = "NG",
) -> int:
    """
    Import the parsed ProtocolSequence into the database.
    Runs inside a clean transaction.
    """
    engine = db_manager.get_engine()
    if engine is None:
        raise ConnectionError("No database connection.")

    step = 0

    # Resolve the real protocol path: the temp copy shares a name with the
    # original, which lives beside the .InletState in the Data directory.
    proto_path = seq.protocol_path
    if seq.inlet_state_path:
        candidate = os.path.join(
            os.path.dirname(seq.inlet_state_path),
            os.path.basename(seq.protocol_path),
        )
        if os.path.isfile(candidate):
            proto_path = candidate

    with engine.begin() as conn:
        existing_lims: dict = {}
        if run_id is not None and delete_existing:
            seq_id = run_id
            if progress_callback:
                progress_callback(step, "Deleting existing data...")

            # Snapshot LIMS-managed fields before deleting, keyed by position.
            # These are never written by the protocol parser and must survive re-import.
            existing_lims = {}
            for row in conn.execute(text("""
                SELECT positioninrun, analysisid, extractionid, iportnumber, status
                FROM ngam.ngpreparations WHERE runid=:sid
            """), {"sid": seq_id}).fetchall():
                existing_lims[row[0]] = {
                    "analysisid":   row[1],
                    "extractionid": row[2],
                    "iportnumber":  row[3],
                    "status":       row[4],
                }

            for sql in (
                "DELETE FROM ngam.ngsignal WHERE iblockid IN ("
                "  SELECT iblockid FROM ngam.ngblock WHERE iheaderid IN ("
                "    SELECT inobleheaderid FROM ngam.ngheaders WHERE runid=:sid))",
                "DELETE FROM ngam.ngblock WHERE iheaderid IN ("
                "  SELECT iblockid FROM ngam.ngblock WHERE iheaderid IN ("
                "    SELECT inobleheaderid FROM ngam.ngheaders WHERE runid=:sid))",
                "DELETE FROM ngam.ngblock WHERE iheaderid IN ("
                "  SELECT inobleheaderid FROM ngam.ngheaders WHERE runid=:sid)",
                "DELETE FROM ngam.ngheaders WHERE runid=:sid",
                "DELETE FROM ngam.ngpreparations WHERE runid=:sid",
            ):
                conn.execute(text(sql), {"sid": seq_id})

            conn.execute(text("""
                UPDATE ngam.ngsequencerun
                SET runstarttime = :ts,
                    runendtime   = :te,
                    nvcprotocolfilepath=:proto, nvcstatusfilepath=:inlet_state,
                    remarks=:desc
                WHERE runid=:sid
            """), {
                "ts": _lv_to_dt(seq.lv_time_start), "te": _lv_to_dt(seq.lv_time_end),
                "proto": proto_path, "inlet_state": seq.inlet_state_path,
                "desc": seq.description, "sid": seq_id,
            })
        else:
            if run_id is not None and delete_existing:
                conn.execute(text("DELETE FROM ngam.ngsequencerun WHERE runid=:sid"), {"sid": run_id})

            row = conn.execute(text("""
                INSERT INTO ngam.msrun
                (runstarttime, runendtime, nvcprotocolfilepath, nvcstatusfilepath,
                 remarks, createdatestamp, createuserstamp, runstatus, measurement_mode)
                VALUES (:ts, :te, :proto, :inlet_state, :desc, NOW(), :user, 1, :mode)
                RETURNING runid
            """), {
                "ts": _lv_to_dt(seq.lv_time_start), "te": _lv_to_dt(seq.lv_time_end),
                "proto": proto_path, "inlet_state": seq.inlet_state_path,
                "desc": seq.description, "user": user_stamp, "mode": measurement_mode,
            }).fetchone()
            seq_id = row[0]

        step += 1
        if progress_callback:
            progress_callback(step, "Importing inlets...")

        for prep in seq.inlets:
            bisblank = "blank" in prep.inlet_string.lower()
            bisreprref = prep.is_reference
            lims = existing_lims.get(prep.seq_num, {})
            prep_row = conn.execute(text("""
                INSERT INTO ngam.ngpreparations
                (runid, positioninrun, nvcinletstring, bisblank,
                 bisreproreference, nvcreferencegas, freferenceamount,
                 flvtimestart, flvtimeend, istepshe, istepsne, istepsar,
                 analysisid, extractionid, iportnumber, status)
                VALUES (:sid, :n, :name, :blank, :ref, :rgas, :ramt, :ts, :te,
                        :steps_he, :steps_ne, :steps_ar,
                        :analysisid, :extractionid, :iportnumber, :status)
                RETURNING inoblepreparationid
            """), {
                "sid": seq_id, "n": prep.seq_num, "name": prep.inlet_string,
                "blank": bisblank, "ref": bisreprref,
                "rgas": prep.reference_gas, "ramt": prep.reference_amount,
                "ts": prep.lv_time_start, "te": prep.lv_time_end,
                "steps_he": prep.partition_steps.get("Helium", 0),
                "steps_ne": prep.partition_steps.get("Neon", 0),
                "steps_ar": prep.partition_steps.get("Argon", 0),
                "analysisid":   lims.get("analysisid"),
                "extractionid": lims.get("extractionid"),
                "iportnumber":  lims.get("iportnumber"),
                "status":       lims.get("status", 0),
            }).fetchone()
            prep_id = prep_row[0]

            step += 1
            if progress_callback:
                progress_callback(step, f"Importing inlet {prep.inlet_string} files...")

            for ms in prep.ms_data:
                if ms.resolved_path is None:
                    step += 1
                    continue
                hdr_row = conn.execute(text("""
                    INSERT INTO ngam.ngheaders
                    (runid, ipreparationid, nvcmeasurementfile,
                     nvcnuclidestoevaluate, nvcinletstring, freferencetime)
                    VALUES (:sid, :prepid, :file, :nuclides, :inlet_str, :ref_time)
                    RETURNING inobleheaderid
                """), {
                    "sid": seq_id, "prepid": prep_id,
                    "file": ms.resolved_path,
                    "nuclides": "\\".join(ms.nuclides),
                    "inlet_str": prep.inlet_string,
                    "ref_time": prep.lv_time_start,
                }).fetchone()
                hdr_id = hdr_row[0]

                step += 1
                if progress_callback:
                    progress_callback(step, f"Importing blocks for {ms.device}...")

                for action, on_faraday in ms.blocks.items():
                    bg_choice = getattr(ms, "bg_proxy_choices", {}).get(action, "original")
                    blk_row = conn.execute(text("""
                        INSERT INTO ngam.ngblock (iheaderid, nvcname, bonfaraday, nvcbackgroundtobeused)
                        VALUES (:hid, :action, :on_faraday, :bg_choice)
                        RETURNING iblockid
                    """), {
                        "hid": hdr_id,
                        "action": action,
                        "on_faraday": on_faraday,
                        "bg_choice": bg_choice
                    }).fetchone()
                    blk_id = blk_row[0]
                    params = [
                        {"bid": blk_id, "t": r.lv_time, "sig": r.signal}
                        for r in ms.signals_for_action(action)
                    ]
                    if params:
                        conn.execute(text("""
                            INSERT INTO ngam.ngsignal (iblockid, flvtime, fsignal)
                            VALUES (:bid, :t, :sig)
                        """), params)

    return seq_id


def _nan_to_none(v: float) -> Optional[float]:
    return None if (v is None or math.isnan(v) or math.isinf(v)) else v


def _best_ccSTP(iso: IsotopeResult) -> Tuple[Optional[float], Optional[float]]:
    """Return (ccSTP, unc) preferring linearity > drift > basic."""
    for val, unc in (
        (iso.linearity_ccSTP, iso.linearity_ccSTP_unc),
        (iso.drift_ccSTP,     iso.drift_ccSTP_unc),
        (iso.ccSTP,           iso.ccSTP_unc),
    ):
        if not (math.isnan(val) or math.isinf(val)):
            return _nan_to_none(val), _nan_to_none(unc)
    return None, None


def save_sequence_results(
    run_id: int,
    result: SequenceProcessingResult,
    conn,
) -> None:
    """
    Persist the processed reduction results for an already-imported run.

    Writes to:
      ngam.ngblockevaluation  — per measurement block: blank, sensitivity, ccSTP
      ngam.ngsequenceevaluation — per run+species: fit degree/window settings
      ngam.ngsequencefit       — polynomial coefficients for blank/drift/linearity fits
    """
    # ── 1. Build block lookup: (positioninrun, nvcname_upper, device_ext_upper) → iblockid
    rows = conn.execute(text("""
        SELECT b.iblockid,
               p.positioninrun,
               UPPER(b.nvcname)                                       AS name_up,
               UPPER(SPLIT_PART(h.nvcmeasurementfile, '.', -1))       AS dev_ext_up
        FROM   ngam.ngblock       b
        JOIN   ngam.ngheaders     h ON h.inobleheaderid = b.iheaderid
        JOIN   ngam.ngpreparations p ON p.inoblepreparationid = h.ipreparationid
        WHERE  p.runid = :run_id
    """), {"run_id": run_id}).fetchall()

    block_map: Dict[Tuple, int] = {}
    for r in rows:
        block_map[(r[1], r[2], r[3])] = r[0]

    # ── 2. Delete stale evaluation rows so we can re-save cleanly
    conn.execute(text("""
        DELETE FROM ngam.ngblockevaluation
        WHERE iblockid IN (
            SELECT b.iblockid FROM ngam.ngblock b
            JOIN ngam.ngheaders h ON h.inobleheaderid = b.iheaderid
            JOIN ngam.ngpreparations p ON p.inoblepreparationid = h.ipreparationid
            WHERE p.runid = :run_id
        )
    """), {"run_id": run_id})
    conn.execute(text("DELETE FROM ngam.ngsequencefit         WHERE runid = :run_id"), {"run_id": run_id})
    conn.execute(text("DELETE FROM ngam.ngsequenceevaluation  WHERE runid = :run_id"), {"run_id": run_id})

    # ── 3. Per-block evaluations
    for inlet in result.inlets:
        for iso_key, iso in inlet.isotopes.items():
            device, isotope = iso_key.split(":", 1)
            lookup = (inlet.seq_num, isotope.upper(), device.upper())
            iblockid = block_map.get(lookup)
            if iblockid is None:
                continue

            ccstp, ccstp_unc = _best_ccSTP(iso)
            # For standards: use directly-computed sensitivity.
            # For unknowns: use the drift-interpolated sensitivity (what was
            # actually used to divide blank-corrected signal → ccSTP).
            _eff = iso.inlet_sensitivity if not math.isnan(iso.inlet_sensitivity) \
                   else iso.drift_sensitivity
            _eff_unc = iso.inlet_sensitivity_unc if not math.isnan(iso.inlet_sensitivity_unc) \
                       else float("nan")
            conn.execute(text("""
                INSERT INTO ngam.ngblockevaluation
                (iblockid,
                 fblank,            fblankuncertainty,
                 fefficiency,       fefficiencyuncertainty,
                 flinearity,        flinearityuncertainty,
                 fccstp,            fccstpuncertainty,
                 fccstppergram,     fccstppergramuncertainty,
                 fvalue,            funcertainty)
                VALUES
                (:bid,
                 :blank,            :blank_unc,
                 :eff,              :eff_unc,
                 :lin,              :lin_unc,
                 :ccstp,            :ccstp_unc,
                 :ppg,              :ppg_unc,
                 :val,              :val_unc)
            """), {
                "bid":       iblockid,
                "blank":     _nan_to_none(iso.blank_net),
                "blank_unc": _nan_to_none(iso.blank_unc),
                "eff":       _nan_to_none(_eff),
                "eff_unc":   _nan_to_none(_eff_unc),
                "lin":       _nan_to_none(iso.linearity_sensitivity),
                "lin_unc":   None,
                "ccstp":     ccstp,
                "ccstp_unc": ccstp_unc,
                "ppg":       _nan_to_none(iso.ccSTP_per_g),
                "ppg_unc":   _nan_to_none(iso.ccSTP_per_g_unc),
                "val":       _nan_to_none(iso.ccSTP_true) if not math.isnan(iso.ccSTP_true) else ccstp,
                "val_unc":   _nan_to_none(iso.ccSTP_true_unc) if not math.isnan(iso.ccSTP_true_unc) else ccstp_unc,
            })

    # ── 4. Sequence-level fit parameters + coefficients
    #       Build a combined species set from all three fit dicts.
    all_species = (
        set(result.blank_fits.keys())
        | set(result.drift_fits.keys())
        | set(result.linearity_fits.keys())
    )

    for species in all_species:
        bf: Optional[BlankFit]     = result.blank_fits.get(species)
        df: Optional[DriftFit]     = result.drift_fits.get(species)
        lf: Optional[LinearityFit] = result.linearity_fits.get(species)

        conn.execute(text("""
            INSERT INTO ngam.ngsequenceevaluation
            (runid, nvcspecies, bfromprep,
             flvfitbstart, flvfitbend,  iblankfitdegree,
             flvfitestart, flvfiteend,  iefficiencyfitdegree,
             flvfitlstart, flvfitlend,  ilinearityfitdegree)
            VALUES
            (:run_id, :species, FALSE,
             :b_start, :b_end,  :b_deg,
             :e_start, :e_end,  :e_deg,
             :l_start, :l_end,  :l_deg)
            ON CONFLICT (runid, nvcspecies) DO UPDATE SET
              flvfitbstart=EXCLUDED.flvfitbstart, flvfitbend=EXCLUDED.flvfitbend,
              iblankfitdegree=EXCLUDED.iblankfitdegree,
              flvfitestart=EXCLUDED.flvfitestart, flvfiteend=EXCLUDED.flvfiteend,
              iefficiencyfitdegree=EXCLUDED.iefficiencyfitdegree,
              flvfitlstart=EXCLUDED.flvfitlstart, flvfitlend=EXCLUDED.flvfitlend,
              ilinearityfitdegree=EXCLUDED.ilinearityfitdegree
        """), {
            "run_id":  run_id,
            "species": species,
            "b_start": _nan_to_none(min(bf.blank_times))  if bf and bf.blank_times else None,
            "b_end":   _nan_to_none(max(bf.blank_times))  if bf and bf.blank_times else None,
            "b_deg":   bf.degree if bf else 0,
            "e_start": _nan_to_none(min(df.std_times))    if df and df.std_times   else None,
            "e_end":   _nan_to_none(max(df.std_times))    if df and df.std_times   else None,
            "e_deg":   df.degree if df else 0,
            "l_start": _nan_to_none(min(lf.signal_levels)) if lf and lf.signal_levels else None,
            "l_end":   _nan_to_none(max(lf.signal_levels)) if lf and lf.signal_levels else None,
            "l_deg":   lf.degree if lf else 0,
        })

        # Coefficients — kind 'B' blank, 'E' drift/efficiency, 'L' linearity
        coeff_rows = []
        for kind, fit in (('B', bf), ('E', df), ('L', lf)):
            if fit is None:
                continue
            for i, c in enumerate(fit.coeffs):
                v = _nan_to_none(c)
                if v is not None:
                    coeff_rows.append({
                        "run_id": run_id, "species": species,
                        "kind": kind, "num": i,
                        "val": v, "unc": None,
                    })
        if coeff_rows:
            conn.execute(text("""
                INSERT INTO ngam.ngsequencefit
                (runid, nvcspecies, ccoefficientkind, icoefficientnumber,
                 fcoefficientvalue, fcoefficientuncertainty)
                VALUES (:run_id, :species, :kind, :num, :val, :unc)
            """), coeff_rows)

    # ── 5. Ratio results (ngratioresult) — requires migration 071
    try:
        conn.execute(text("DELETE FROM ngam.ngratioresult WHERE runid = :run_id"), {"run_id": run_id})
        ratio_rows = []
        for inlet in result.inlets:
            for rr in inlet.ratio_results.values():
                ratio_rows.append({
                    "run_id": run_id,
                    "pos":    inlet.seq_num,
                    "name":   rr.ratio_name,
                    "raw":    _nan_to_none(rr.raw_ratio),
                    "raw_u":  _nan_to_none(rr.raw_ratio_unc),
                    "bc":     _nan_to_none(rr.blank_corrected),
                    "bc_u":   _nan_to_none(rr.blank_corrected_unc),
                    "dc":     _nan_to_none(rr.drift_corrected),
                    "dc_u":   _nan_to_none(rr.drift_corrected_unc),
                })
        if ratio_rows:
            conn.execute(text("""
                INSERT INTO ngam.ngratioresult
                (runid, positioninrun, ratio_name,
                 raw_ratio, raw_ratio_unc,
                 blank_corrected, blank_corrected_unc,
                 drift_corrected, drift_corrected_unc)
                VALUES (:run_id, :pos, :name, :raw, :raw_u, :bc, :bc_u, :dc, :dc_u)
                ON CONFLICT (runid, positioninrun, ratio_name) DO UPDATE SET
                  raw_ratio=EXCLUDED.raw_ratio, raw_ratio_unc=EXCLUDED.raw_ratio_unc,
                  blank_corrected=EXCLUDED.blank_corrected,
                  blank_corrected_unc=EXCLUDED.blank_corrected_unc,
                  drift_corrected=EXCLUDED.drift_corrected,
                  drift_corrected_unc=EXCLUDED.drift_corrected_unc
            """), ratio_rows)
    except Exception:
        pass  # table absent (migration 071 not yet applied) — skip silently

    # ── 6. Gauge-derived concentrations (nggaugeresult) — requires migration 071
    try:
        conn.execute(text("DELETE FROM ngam.nggaugeresult WHERE runid = :run_id"), {"run_id": run_id})
        gauge_rows = []
        for inlet in result.inlets:
            if not getattr(inlet, "gauge_conc", None):
                continue
            for element, conc in inlet.gauge_conc.items():
                gauge_rows.append({
                    "run_id":  run_id,
                    "pos":     inlet.seq_num,
                    "element": element,
                    "conc":    _nan_to_none(conc),
                    "conc_u":  _nan_to_none(inlet.gauge_conc_unc.get(element, float("nan"))),
                    "ppg":     _nan_to_none(inlet.gauge_conc_per_g.get(element, float("nan"))),
                    "ppg_u":   _nan_to_none(inlet.gauge_conc_per_g_unc.get(element, float("nan"))),
                })
        if gauge_rows:
            conn.execute(text("""
                INSERT INTO ngam.nggaugeresult
                (runid, positioninrun, element, conc, conc_unc, conc_per_g, conc_per_g_unc)
                VALUES (:run_id, :pos, :element, :conc, :conc_u, :ppg, :ppg_u)
                ON CONFLICT (runid, positioninrun, element) DO UPDATE SET
                  conc=EXCLUDED.conc, conc_unc=EXCLUDED.conc_unc,
                  conc_per_g=EXCLUDED.conc_per_g,
                  conc_per_g_unc=EXCLUDED.conc_per_g_unc
            """), gauge_rows)
    except Exception:
        pass  # table absent — skip silently
