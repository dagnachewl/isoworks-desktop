"""
trims_electrolysis_dal.py — Data Access Layer for TRIMS Electrolysis Run Creation.
Handles all database interactions for the TrimsElectrolysisCreateRunWidget.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple, Dict, Any

from sqlalchemy import text
from db_core import db_manager

# --- Data Structures for Type Hinting ---

@dataclass
class ControlSample:
    sample_id: int
    prefix: str
    name: str
    description: str

@dataclass
class Workflow:
    id: int
    name: str

@dataclass
class Procedure:
    id: int
    name: str

@dataclass
class System:
    id: int
    name: str

@dataclass
class Cell:
    cell_id: int
    is_obsolete: bool
    lock_for_unknowns: bool

@dataclass
class AvailableSample:
    sample_id: int
    prefix: str
    name: str
    our_lab_id: str
    priority_id: int
    submission_id: int
    analysis_id: int

@dataclass
class RunCreationPayload:
    run_id: int
    procedure_id: int
    workflow_id: int
    system_id: int
    user: str
    start_time: datetime
    load_list: List[Dict[str, Any]]

# --- Read Operations ---

def get_control_samples_for_dialog() -> List[ControlSample]:
    """Fetches active control samples (Spike, Blank, Control) for the 'Add Control' dialog."""
    sql = """
        SELECT rc.SampleID, rc.Prefix, s.sName, st.strLongDescription
        FROM Sample s
        JOIN ReferenceControl rc
          ON s.SampleID = rc.SampleID AND s.Prefix = rc.Prefix
        JOIN ReferenceControlData rcd
          ON rc.ReferenceID = rcd.ReferenceID
        JOIN tblSampleType st
          ON st.intSampleType = s.SampleType
        WHERE rcd.AvailableDateTo IS NULL
          AND s.SampleType IN (3, 6, 7)
        ORDER BY s.SampleType, rc.SampleID
    """
    with db_manager.get_connection() as conn:
        rows = conn.execute(text(sql)).fetchall()
    return [ControlSample(r[0], r[1], r[2], r[3]) for r in rows]

def get_electrolysis_workflows() -> List[Workflow]:
    """Fetches workflows that include an 'Electrolysis_Enrichment' job."""
    sql = f"""
        SELECT DISTINCT w.WorkflowID,
               {db_manager.sql_concat("w.WorkflowID", "' - '", "w.WorkflowName")}
        FROM Workflow w
        JOIN WorkflowJob wj ON w.WorkflowID = wj.WorkflowID
        WHERE wj.JobName LIKE 'Electrolysis_Enrichment%'
        ORDER BY w.WorkflowID
    """
    with db_manager.get_connection() as conn:
        rows = conn.execute(text(sql)).fetchall()
    return [Workflow(r[0], r[1]) for r in rows]

def get_enrichment_procedures() -> List[Procedure]:
    """Fetches all active enrichment procedures."""
    sql = f"""
        SELECT p.ProcedureID,
               {db_manager.sql_concat('p.ProcedureID', "' - '", 'p.ProcedureName')}
        FROM AnalysisProcedure p
        JOIN EnrichmentProcedure ep ON p.ProcedureID = ep.ProcedureID
        WHERE p.IsObsolete = {db_manager.sql_bool(False)}
        ORDER BY p.ProcedureID
    """
    with db_manager.get_connection() as conn:
        rows = conn.execute(text(sql)).fetchall()
    return [Procedure(r[0], r[1]) for r in rows]

def get_procedure_for_workflow(workflow_id: int) -> Optional[int]:
    """Finds the default ProcedureID for the electrolysis job in a workflow."""
    sql = """
        SELECT ProcedureID
        FROM WorkflowJob
        WHERE WorkflowID = :wid AND JobName LIKE 'Electrolysis_Enrichment%'
    """
    with db_manager.get_connection() as conn:
        result = conn.execute(text(sql), {"wid": workflow_id}).scalar()
    return int(result) if result is not None else None

def get_procedure_details(procedure_id: int) -> Optional[Tuple[float, int]]:
    """Fetches SampleSize and DefaultDeviceID for a procedure."""
    sql = "SELECT SampleSize, DefaultDeviceID FROM AnalysisProcedure WHERE ProcedureID = :pid"
    with db_manager.get_connection() as conn:
        row = conn.execute(text(sql), {"pid": procedure_id}).fetchone()
    return (float(row[0] or 0), row[1]) if row else None

def get_systems_for_procedure(sample_size: float) -> List[System]:
    """Fetches electrolysis systems compatible with a given sample size."""
    sql = f"""
        SELECT e.EquipmentID,
               {db_manager.sql_concat('e.EquipmentID', "' - '", 'e.EquipmentName')}
        FROM Equipment e
        JOIN ElectrolysisSystem es ON e.EquipmentID = es.ElysSystemID
        WHERE e.CategoryID = 2 AND es.SampleVolume >= :size
        ORDER BY e.EquipmentID
    """
    with db_manager.get_connection() as conn:
        rows = conn.execute(text(sql), {"size": sample_size}).fetchall()
    return [System(r[0], r[1]) for r in rows]

def get_enrichment_config(procedure_id: int) -> Optional[Dict[str, Any]]:
    """Fetches spike/blank counts and default IDs from EnrichmentProcedure."""
    sql = """
        SELECT NumberOfSpike, NumberOfDeadWater, SpikeID, DeadWaterID
        FROM EnrichmentProcedure WHERE ProcedureID = :pid
    """
    with db_manager.get_connection() as conn:
        row = conn.execute(text(sql), {"pid": procedure_id}).fetchone()
        if not row:
            return None
        return {
            "spikes": row[0] or 0,
            "blanks": row[1] or 0,
            "spike_id_default": row[2],
            "blank_id_default": row[3],
        }

def get_valid_control_id(default_id: Optional[int], sample_type: int) -> Optional[Tuple[int, str, str]]:
    """
    Finds an active control sample, preferring default_id.
    Returns (SampleID, Prefix, sName) or None.
    """
    with db_manager.get_connection() as conn:
        if default_id:
            row = conn.execute(text("""
                SELECT rc.SampleID, s.Prefix, s.sName
                FROM ReferenceControlData rcd
                JOIN ReferenceControl rc ON rcd.ReferenceID = rc.ReferenceID
                JOIN Sample s ON rc.SampleID = s.SampleID AND rc.Prefix = s.Prefix
                WHERE rc.SampleID = :sid AND rcd.AvailableDateTo IS NULL
            """), {"sid": default_id}).fetchone()
            if row:
                return row[0], row[1], row[2]

        sql_fb = f"""
            SELECT {db_manager.sql_top(1)} rc.SampleID, s.Prefix, s.sName
            FROM ReferenceControl rc
            JOIN ReferenceControlData rcd ON rc.ReferenceID = rcd.ReferenceID
            JOIN Sample s ON rc.SampleID = s.SampleID AND rc.Prefix = s.Prefix
            WHERE s.SampleType = :stype AND rcd.AvailableDateTo IS NULL
            ORDER BY rc.SampleID
            {db_manager.sql_limit(1)}
        """
        row = conn.execute(text(sql_fb), {"stype": sample_type}).fetchone()
        return (row[0], row[1], row[2]) if row else None

def get_system_cell_config(system_id: int) -> Dict[str, Any]:
    """Fetches cell count and a list of all cell properties for a system."""
    with db_manager.get_connection() as conn:
        row = conn.execute(text(
            "SELECT NumberOfPositions FROM Equipment WHERE EquipmentID=:id"
        ), {"id": system_id}).fetchone()
        equip_count = row[0] or 0 if row else 0

        cell_rows = conn.execute(text("""
            SELECT CellID, COALESCE(IsObsolete, FALSE), COALESCE(LockForUnknowns, FALSE)
            FROM ElectrolysisCell WHERE SystemID = :sid ORDER BY CellID
        """), {"sid": system_id}).fetchall()

        cells = [Cell(r[0], bool(r[1]), bool(r[2])) for r in cell_rows]
        return {"count": equip_count or len(cells), "cells": cells}

def get_last_rotation_ids(system_id: int, spike_info: tuple, blank_info: tuple) -> Dict[str, int]:
    """Fetches the last used CellID for spikes and blanks on a given system."""
    from trims_electrolysis_create_run_gui import NO_LAST_CELL
    result = {"last_spiked_cell": NO_LAST_CELL, "last_dw_cell": NO_LAST_CELL}
    with db_manager.get_connection() as conn:
        last_run_id = conn.execute(text(
            "SELECT MAX(RunID) FROM TRIMS.ElectrolysisRun WHERE ElysSystemID=:sid"
        ), {"sid": system_id}).scalar()

        if not last_run_id:
            return result

        spike_sid, spike_pfx, _ = spike_info
        if spike_sid and spike_pfx:
            r = conn.execute(text("""
                SELECT MAX(e.CellID) FROM TRIMS.Electrolysis e
                JOIN Analysis a ON e.AnalysisID = a.AnalysisID
                WHERE e.RunID = :rid AND a.SampleID = :sid AND a.Prefix = :pfx
            """), {"rid": last_run_id, "sid": spike_sid, "pfx": spike_pfx}).scalar()
            if r: result["last_spiked_cell"] = r

        dw_sid, dw_pfx, _ = blank_info
        if dw_sid and dw_pfx:
            r = conn.execute(text("""
                SELECT MAX(e.CellID) FROM TRIMS.Electrolysis e
                JOIN Analysis a ON e.AnalysisID = a.AnalysisID
                WHERE e.RunID = :rid AND a.SampleID = :sid AND a.Prefix = :pfx
            """), {"rid": last_run_id, "sid": dw_sid, "pfx": dw_pfx}).scalar()
            if r: result["last_dw_cell"] = r
    return result

def get_next_electrolysis_run_id() -> int:
    """Returns an estimated next RunID for display purposes; actual ID assigned by sequence at INSERT."""
    with db_manager.get_connection() as conn:
        max_id = conn.execute(text("SELECT MAX(RunID) FROM TRIMS.ElectrolysisRun")).scalar()
        return (max_id or 1000) + 1

def get_available_samples(workflow_id: int, status: int) -> List[AvailableSample]:
    """Fetches samples ready for enrichment for a given workflow and status."""
    sql = f"""
        SELECT s.SampleID, s.Prefix, s.sName,
               {db_manager.sql_concat("s.Prefix", "'-'", "s.SampleID")},
               sub.PriorityID, sub.SubmissionID, a.AnalysisID
        FROM Sample s
        JOIN Submission sub ON s.SubmissionID = sub.SubmissionID
        JOIN Analysis a ON s.SampleID = a.SampleID AND s.Prefix = a.Prefix
        WHERE a.WorkflowID = :wid
    """
    params = {"wid": workflow_id}
    if status != -1:
        sql += " AND a.Status = :stat"
        params["stat"] = status
    sql += " ORDER BY sub.PriorityID DESC, s.SampleID"

    with db_manager.get_connection() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [AvailableSample(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows]

# --- Write Operations ---

def create_electrolysis_run(payload: RunCreationPayload):
    """Creates a new electrolysis run and all its associated records in a single transaction."""
    from trims_electrolysis_create_run_gui import CELL_UNKNOWN

    with db_manager.get_connection() as conn:
        with conn.begin():  # Start a transaction
            # 1. Get WorkflowJobID
            wjid = None
            if payload.workflow_id:
                wjid = conn.execute(text(
                    "SELECT WorkflowJobID FROM WorkflowJob "
                    "WHERE WorkflowID=:w AND JobName LIKE 'Electrolysis_Enrichment%'"
                ), {"w": payload.workflow_id}).scalar()

            # 2. Insert into TRIMS.ElectrolysisRun — sequence assigns RunID
            actual_run_id = conn.execute(text("""
                INSERT INTO TRIMS.ElectrolysisRun
                    (ProcedureID, WorkflowID, WorkflowJobID, ElysSystemID,
                     RunStartTime, CreateDateStamp, CreateUserStamp, RunStatus)
                VALUES (:pid, :wid, :wjid, :eid, :now, :now, :user, 4)
                RETURNING RunID
            """), {
                "pid": payload.procedure_id, "wid": payload.workflow_id,
                "wjid": wjid, "eid": payload.system_id, "now": payload.start_time, "user": payload.user
            }).scalar()

            # 4. Pre-fetch SampleTypes for all samples in the load list
            load_pairs = [(item['sid'], item['pfx']) for item in payload.load_list if item.get('sid')]
            stype_map = {}
            if load_pairs:
                # This is a simplified approach. For SQL Server, a more complex query
                # with a temporary table or multiple IN clauses might be needed if the
                # list is very large. For a typical run size, this is fine.
                for sid_val, pfx_val in set(load_pairs):
                    stype = conn.execute(text(
                        "SELECT SampleType FROM Sample WHERE SampleID = :sid AND Prefix = :pfx"
                    ), {"sid": sid_val, "pfx": pfx_val}).scalar()
                    if stype is not None:
                        stype_map[(sid_val, pfx_val)] = stype

            # 5. Loop through load list and insert into Analysis and TRIMS.Electrolysis
            for item in payload.load_list:
                sid, pfx, ctype = item.get('sid'), item.get('pfx'), item.get('ctype')
                if not sid:
                    continue

                db_stype = stype_map.get((sid, pfx), CELL_UNKNOWN)

                if ctype == CELL_UNKNOWN:
                    # Unknown sample: AnalysisID already exists, just update its status
                    current_aid = item['aid']
                    conn.execute(text("UPDATE Analysis SET Status = 4 WHERE AnalysisID = :aid"), {"aid": current_aid})
                else:
                    # Control/Spike/Blank: Create a new Analysis record — sequence assigns ID
                    current_aid = conn.execute(text("""
                        INSERT INTO Analysis (SampleID, Prefix, Status, Repeats,
                                     WorkflowID, CreateDateStamp, CreateUserStamp)
                        VALUES (:sid, :pfx, 4, 1, :wid, :now, :user)
                        RETURNING AnalysisID
                    """), {
                        "sid": sid, "pfx": pfx, "wid": payload.workflow_id,
                        "now": payload.start_time, "user": payload.user
                    }).scalar()

                # Insert into the main electrolysis table
                conn.execute(text("""
                    INSERT INTO TRIMS.Electrolysis (CellID, AnalysisID, RunID,
                                 CreateDateStamp, CreateUserStamp, SampleType)
                    VALUES (:cell, :aid, :rid, :now, :user, :stype)
                """), {
                    "cell": item['cell_id'], "aid": current_aid, "rid": actual_run_id,
                    "now": payload.start_time, "user": payload.user, "stype": db_stype
                })
    # The transaction is automatically committed on exiting the 'with conn.begin()' block
    # or rolled back if an exception occurs.