"""
Protocol Manager - Simplified for FileFormat Pattern
=====================================================

CORE DESIGN:
- Protocol.settings['file_format_id'] links to GUItblFileFormat.lngFormatID
- FileFormat table defines instrument (strInstrumentName, strInstrumentModel)
- Settings JSON contains only pipeline configuration
- No instrument_name in settings (comes from FileFormat)

Database Schema:
- Protocol: ProtocolID, Name, Module, SettingsJSON, IsDefault, IsActive
- ProtocolMapping: ProtocolID, TargetField, SourceHeader, IsNet, DisplayOrder
- GUItblFileFormat: lngFormatID, strFormatName, strInstrumentName, intCategory
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime
from sqlalchemy import text
from db_core import db_manager
from smart_formatting import format_snapshot_parameters

@dataclass
class ColumnMapping:
    """Column mapping from file to database"""
    target_field: str
    source_header: str
    is_net: bool = False
    uncertainty_column: Optional[str] = None
    display_order: int = 0
    notes: Optional[str] = None


@dataclass
class Protocol:
    """
    Simplified Protocol Object
    
    Key Design:
        format_id → Protocol.FormatID → GUItblFileFormat.lngFormatID
        FileFormat defines instrument
        Settings = pipeline config only (NO format_id in JSON anymore!)
    
    Example settings:
    {
        "linearity_correction": "None",
        "memory_correction": "Fast/Slow Pool",
        "drift_correction": "Linear",        
        "calibration_method": "TwoPoint",
        "min_injections": 5,
        "water_conc_min": 5000,
        "water_conc_max": 25000
    }
    """
    id: int = -1
    name: str = "New Protocol"
    module: str = "SIAM"
    format_id: Optional[int] = None  # Stored in Protocol.FormatID column!
    description: str = ""
    is_active: bool = True
    is_default: bool = False
    settings: Dict[str, Any] = field(default_factory=dict)
    mappings: List[ColumnMapping] = field(default_factory=list)
    
    # Audit
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    modified_by: Optional[str] = None
    modified_at: Optional[datetime] = None
    
    def get_file_format_id(self) -> Optional[int]:
        """Get file format ID from direct attribute (NOT from settings!)"""
        return self.format_id
    
    def set_file_format_id(self, format_id: int):
        """Set file format ID in direct attribute"""
        self.format_id = format_id

class ProtocolManager:
    """
    Simplified Protocol Manager - FileFormat Pattern
    
    Procedures (and their underlying Protocols) define the rules, mathematical settings,
    and column mappings used when parsing and correcting analytical data. They form the
    bridge between raw instrument output and final LIMS calculations.
    
    The Protocol Architecture:
    Behind the scenes, a Protocol consists of two main components:
    1. Settings (JSON): The mathematical configuration for the pipeline.
       - SIAM: Linearity models, Memory carryover method, Drift correction axis, and Outlier detection methods.
       - LSC: Signal Metric, Efficiency Source, Background Mode, Activity Unit.
    2. Column Mappings: Maps raw file headers to expected IsoWorks database fields.
       - e.g., Mapping the file header "CPMroi1" to the target field "CPM".
       - Includes flags for whether the mapped column is already *Net* (background-subtracted).
       
    Audit Trail:
    Whenever an analyst processes a run, the system takes an immutable JSON snapshot of
    the Protocol at that exact moment. This guarantees the mathematical history of a run
    is preserved even if the central Protocol is later edited.
    """
    
    # Module to category mapping (used throughout)
    MODULE_CATEGORY_MAP = {
        'LSC': 3,
        'SIAM': 5,
        'NGAM': 13
    }
    
    @staticmethod
    def _get_category(module: str) -> Optional[int]:
        """Convert module string to category int"""
        return ProtocolManager.MODULE_CATEGORY_MAP.get(module)
    
    @staticmethod
    def save_protocol(protocol: Protocol, user: str = 'SYSTEM') -> int:
        """
        Save protocol to database
        
        Args:
            protocol: Protocol object
            user: Username for audit
            
        Returns:
            Protocol ID
        """
        # Check if database is ready
        if not hasattr(db_manager, '_engine') or db_manager._engine is None:
            raise RuntimeError("Database not ready, cannot save protocol")
                 
        # Validate format_id is set (CRITICAL!)
        if protocol.format_id is None:
            logging.warning(f"Protocol has no format_id - won't be queryable by format")
        
        with db_manager.get_connection() as conn:
            trans = conn.begin()
            try:
                settings_json = json.dumps(protocol.settings)
                
                now_func = "NOW()" if db_manager.dialect == "POSTGRESQL" else "GETDATE()"
                if protocol.id > 0:
                    # Update existing
                    conn.execute(text(f"""
                        UPDATE Protocol
                        SET Name=:name, Module=:mod, Description=:desc,
                            SettingsJSON=:json, FormatID=:fid, IsActive=:act, IsDefault=:def,
                            ModifiedBy=:user, ModifiedAt={now_func}
                        WHERE ProtocolID=:pid
                    """), {
                        "name": protocol.name,
                        "mod": protocol.module,
                        "desc": protocol.description,
                        "json": settings_json,
                        "fid": protocol.format_id,  # Validated above!
                        "act": bool(protocol.is_active),
                        "def": bool(protocol.is_default),
                        "user": user,
                        "pid": protocol.id
                    })
                    pid = protocol.id
                    
                    # Unset other defaults for SAME module AND format
                    if protocol.is_default:
                        conn.execute(text(f"""
                            UPDATE Protocol
                            SET IsDefault = {db_manager.sql_bool(False)}
                            WHERE Module = :mod
                              AND FormatID = :fid
                              AND ProtocolID != :pid
                        """), {"mod": protocol.module, "fid": protocol.format_id, "pid": pid})
                else:
                    # Insert new
                    if protocol.is_default:
                        # Unset other defaults for SAME module AND format
                        conn.execute(text(f"""
                            UPDATE Protocol
                            SET IsDefault = {db_manager.sql_bool(False)}
                            WHERE Module = :mod
                              AND FormatID = :fid
                        """), {"mod": protocol.module, "fid": protocol.format_id})
                    
                    if db_manager.dialect == "POSTGRESQL":
                        insert_sql = """
                            INSERT INTO Protocol (Name, Module, Description, SettingsJSON, FormatID, IsActive, IsDefault, CreatedBy)
                            VALUES (:name, :mod, :desc, :json, :fid, :act, :def, :user)
                            RETURNING ProtocolID
                        """
                    else:
                        insert_sql = """
                            INSERT INTO Protocol (Name, Module, Description, SettingsJSON, FormatID, IsActive, IsDefault, CreatedBy)
                            OUTPUT INSERTED.ProtocolID
                            VALUES (:name, :mod, :desc, :json, :fid, :act, :def, :user)
                        """
                    res = conn.execute(text(insert_sql), {
                        "name": protocol.name,
                        "mod": protocol.module,
                        "desc": protocol.description,
                        "json": settings_json,
                        "fid": protocol.format_id,
                        "act": bool(protocol.is_active),
                        "def": bool(protocol.is_default),
                        "user": user
                    })
                    pid = res.scalar()
                    protocol.id = pid

                # Save Mappings
                conn.execute(text("DELETE FROM ProtocolMapping WHERE ProtocolID = :pid"), {"pid": pid})
                
                if protocol.mappings:
                    vals = []
                    for idx, m in enumerate(protocol.mappings):
                        vals.append({
                            "pid": pid, 
                            "tf": m.target_field, 
                            "sh": m.source_header or '',
                            "unc": m.uncertainty_column,
                            "net": 1 if m.is_net else 0, 
                            "ord": m.display_order or idx,
                            "notes": m.notes
                        })
                    
                    conn.execute(text("""
                        INSERT INTO ProtocolMapping 
                        (ProtocolID, TargetField, SourceHeader, UncertaintyColumn, 
                        IsNet, DisplayOrder, Notes)
                        VALUES (:pid, :tf, :sh, :unc, :net, :ord, :notes)
                    """), vals)

                trans.commit()
                logging.info(f"Saved protocol {pid}: {protocol.name} (format_id={protocol.format_id})")
                return pid
                
            except Exception as e:
                trans.rollback()
                logging.error(f"Protocol save error: {e}", exc_info=True)
                raise
    
    @staticmethod
    def load_protocol(protocol_id: int) -> Optional[Protocol]:
        """Load protocol by ID"""
        try:
            # Check if database is ready
            if not hasattr(db_manager, '_engine') or db_manager._engine is None:
                logging.info("Database not ready yet, cannot load protocol")
                return None
            
            with db_manager.get_connection() as conn:
                # Load header
                row = conn.execute(text("""
                    SELECT ProtocolID, Name, Module, Description, SettingsJSON, FormatID,
                           IsActive, IsDefault, CreatedBy, CreatedAt, ModifiedBy, ModifiedAt
                    FROM Protocol
                    WHERE ProtocolID = :pid
                """), {"pid": protocol_id}).fetchone()
                
                if not row:
                    return None
                
                # Use index access for SQL Server compatibility
                try:
                    protocol_id_val = row[0]  # ProtocolID
                    name = row[1]  # Name
                    module = row[2]  # Module
                    description = row[3] if row[3] else ''  # Description
                    settings_json = row[4]  # SettingsJSON
                    format_id = row[5]  # FormatID                    
                    is_active = bool(row[6])  # IsActive
                    is_default = bool(row[7])  # IsDefault
                    created_by = row[8]  # CreatedBy
                    created_at = row[9]  # CreatedAt
                    modified_by = row[10]  # ModifiedBy
                    modified_at = row[11]  # ModifiedAt
                except (IndexError, TypeError) as e:
                    logging.error(f"Could not parse protocol row: {e}")
                    return None
                
                # Parse settings
                settings = json.loads(settings_json) if settings_json else {}                
                # Load mappings
                mapping_rows = conn.execute(text("""
                    SELECT TargetField, SourceHeader, UncertaintyColumn, IsNet, DisplayOrder, Notes
                    FROM ProtocolMapping
                    WHERE ProtocolID = :pid
                    ORDER BY DisplayOrder
                """), {"pid": protocol_id}).fetchall()
                
                # Parse mappings using index access
                mappings = []
                for m in mapping_rows:
                    try:
                        mappings.append(ColumnMapping(
                            target_field=m[0],  # TargetField
                            source_header=m[1] if m[1] else '',  # SourceHeader
                            uncertainty_column=m[2] if m[2] else None,
                            is_net=bool(m[3]),  # IsNet
                            display_order=m[4] if m[4] else 0,  # DisplayOrder
                            notes=m[5]  # Notes
                        ))
                    except (IndexError, TypeError) as e:
                        logging.warning(f"Could not parse mapping row: {e}")
                        continue
                
                # Create protocol
                protocol = Protocol(
                    id=protocol_id_val,
                    name=name,
                    module=module,
                    format_id=format_id,
                    description=description,
                    is_active=is_active,
                    is_default=is_default,
                    settings=settings,
                    mappings=mappings,
                    created_by=created_by,
                    created_at=created_at,
                    modified_by=modified_by,
                    modified_at=modified_at
                )
                
                return protocol
                
        except Exception as e:
            logging.error(f"Protocol load error: {e}", exc_info=True)
            return None

    @staticmethod
    def get_mapping_dict(protocol: Protocol) -> dict:
        """
        Convert protocol mappings to dict {target_field: source_header}.
        
        This method provides backward compatibility with old code
        that expects a simple dict of target->source mappings.
        
        Args:
            protocol: Protocol object with mappings
            
        Returns:
            Dictionary mapping target fields to source headers
            Example: {'CPM': 'CPM_1', 'DPM': 'DPM_1', ...}
        """
        if not protocol or not protocol.mappings:
            return {}
        
        return {m.target_field: m.source_header for m in protocol.mappings}

    @staticmethod
    def save_run_protocol_snapshot(
        run_id: int,
        protocol,  # Protocol object or None
        module: str,
        fit_parameters: Optional[Dict[str, Any]] = None,
        was_modified: bool = False,
        user: str = 'SYSTEM'
    ) -> bool:
        """
        Save complete protocol snapshot for audit trail.
        
        Creates a permanent record of protocol configuration used for each run.
        Saves to existing RunProtocolSnapshot table with single JSON column.
        
        Args:
            run_id: Run ID to associate snapshot with
            protocol: Protocol object (or None for ad-hoc settings)
            module: Module name (LSC, SIAM, NGAM)
            fit_parameters: Computed values from analysis
                Example: {
                    'background_cpm': 12.5,
                    'background_cpm_uncertainty': 0.3,
                    'efficiency': 0.45,
                    'efficiency_uncertainty': 0.02,
                    'chi_squared': 1.2,
                    'outliers_removed': 2,
                    'samples_processed': 24
                }
            was_modified: True if protocol was modified from saved version
            user: Username who performed the analysis
            
        Returns:
            True if saved successfully, False otherwise
        """
        try:
            # Validate inputs
            if not run_id or run_id <= 0:
                logging.warning("Cannot save snapshot: Invalid run_id")
                return False
            
            # Get protocol details
            if protocol:
                protocol_id = protocol.id if hasattr(protocol, 'id') and protocol.id > 0 else None
                protocol_name = protocol.name if hasattr(protocol, 'name') else 'Unknown'
                settings = protocol.settings if hasattr(protocol, 'settings') else {}
                mappings = protocol.mappings if hasattr(protocol, 'mappings') else []
            else:
                protocol_id = None
                protocol_name = f"Ad-hoc ({module})"
                settings = {}
                mappings = []
            
            # Build complete snapshot as single JSON object
            snapshot = {
                'protocol_name': protocol_name,
                'settings': settings,
                'mappings': [],
                'fit_parameters': []
            }
            
            # Serialize mappings
            if mappings:
                for m in mappings:
                    mapping_dict = {
                        'target_field': m.target_field,
                        'source_header': m.source_header,
                        'is_net': m.is_net,
                        'display_order': getattr(m, 'display_order', 0),
                    }
                    
                    # Add optional fields if they exist
                    if hasattr(m, 'uncertainty_column') and m.uncertainty_column:
                        mapping_dict['uncertainty_column'] = m.uncertainty_column
                    
                    if hasattr(m, 'notes') and m.notes:
                        mapping_dict['notes'] = m.notes
                    
                    snapshot['mappings'].append(mapping_dict)
            
            # Convert entire snapshot to JSON            
            formatted_fit_params = format_snapshot_parameters(fit_parameters) if fit_parameters else None            
            snapshot['fit_parameters'].append(formatted_fit_params)
            snapshot_json = json.dumps(snapshot, indent=2)
            
            if not hasattr(db_manager, '_engine') or db_manager._engine is None:
                logging.warning("Database not ready, cannot save protocol snapshot")
                return False
            
            with db_manager.get_connection() as conn:
                try:
                    now_func = "NOW()" if db_manager.dialect == "POSTGRESQL" else "GETDATE()"
                    params = {
                        'protocol_id': protocol_id,
                        'module': module,
                        'run_id': run_id,
                        'snapshot': snapshot_json,
                        'was_modified': was_modified,
                        'user': user
                    }
                    # UPSERT: Insert if new, Update if exists
                    if db_manager.dialect == "POSTGRESQL":
                        upsert_sql = f"""
                            INSERT INTO RunProtocolSnapshot
                                (ProtocolID, Module, RunID, ProtocolSnapshot, WasModified, AppliedBy, AppliedAt)
                            VALUES
                                (:protocol_id, :module, :run_id, :snapshot, :was_modified, :user, {now_func})
                            ON CONFLICT (RunID, Module) DO UPDATE SET
                                ProtocolID = EXCLUDED.ProtocolID,
                                ProtocolSnapshot = EXCLUDED.ProtocolSnapshot,
                                WasModified = EXCLUDED.WasModified,
                                AppliedBy = EXCLUDED.AppliedBy,
                                AppliedAt = {now_func}
                        """
                    else:
                        upsert_sql = f"""
                            MERGE RunProtocolSnapshot AS target
                            USING (
                                SELECT
                                    :protocol_id AS ProtocolID,
                                    :module AS Module,
                                    :run_id AS RunID,
                                    :snapshot AS ProtocolSnapshot,
                                    :was_modified AS WasModified,
                                    :user AS AppliedBy
                            ) AS source
                            ON target.RunID = source.RunID AND target.Module = source.Module
                            WHEN MATCHED THEN
                                UPDATE SET
                                    ProtocolID = source.ProtocolID,
                                    ProtocolSnapshot = source.ProtocolSnapshot,
                                    WasModified = source.WasModified,
                                    AppliedBy = source.AppliedBy,
                                    AppliedAt = {now_func}
                            WHEN NOT MATCHED THEN
                                INSERT (ProtocolID, Module, RunID, ProtocolSnapshot, WasModified, AppliedBy, AppliedAt)
                                VALUES (source.ProtocolID, source.Module, source.RunID,
                                        source.ProtocolSnapshot, source.WasModified,
                                        source.AppliedBy, {now_func});
                        """
                    result = conn.execute(text(upsert_sql), params)                                
                    conn.commit()
                                    
                except Exception as merge_error:
                    logging.error(f"DEBUG: MERGE failed: {merge_error}", exc_info=True)
                    return False
                
                logging.info(f"✓ Saved protocol snapshot: Run {run_id}, Protocol '{protocol_name}' ({module})")
                
        except Exception as e:
            logging.error(f"Failed to save protocol snapshot for run {run_id}: {e}", exc_info=True)
            return False
                
    @staticmethod
    def get_run_protocol_snapshot(run_id: int, module: str = 'LSC') -> Optional[Protocol]:
        """
        Retrieve the protocol snapshot saved for a run from public.RunProtocolSnapshot.

        Returns a Protocol whose .mappings list mirrors what was applied at process time.
        Returns None if no snapshot exists or the DB is not ready.
        """
        try:
            if not hasattr(db_manager, '_engine') or db_manager._engine is None:
                return None
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT ProtocolID, ProtocolSnapshot
                    FROM RunProtocolSnapshot
                    WHERE RunID = :rid AND Module = :mod
                """), {"rid": run_id, "mod": module}).fetchone()
                if not row or not row[1]:
                    return None
                snap = json.loads(row[1])
                mappings = [
                    ColumnMapping(
                        target_field=m.get("target_field", ""),
                        source_header=m.get("source_header", ""),
                        is_net=bool(m.get("is_net", False)),
                    )
                    for m in snap.get("mappings", [])
                ]
                s = snap.get("settings", {})
                protocol = Protocol(
                    id=row[0] or -1,
                    name=snap.get("protocol_name", ""),
                    module=module,
                    settings=s,
                    mappings=mappings,
                )
                return protocol
        except Exception as e:
            logging.warning(f"get_run_protocol_snapshot failed for run {run_id}: {e}")
            return None

    @staticmethod
    def get_default_protocol(module: str, file_format_id: Optional[int] = None) -> Optional[Protocol]:
        """
        Get default protocol for module, optionally filtered by file format
        
        Args:
            module: 'LSC', 'SIAM', or 'NGAM'
            file_format_id: Optional file format ID to match
            
        Returns:
            Protocol or None
        """
        try:
            # Check if database is ready
            if not hasattr(db_manager, '_engine') or db_manager._engine is None:
                logging.info("Database not ready yet, cannot load default protocol")
                return None
            
            with db_manager.get_connection() as conn:
                # Query with optional FormatID filter (MUCH MORE EFFICIENT!)
                if file_format_id is not None:
                    row = conn.execute(text(f"""
                        SELECT ProtocolID
                        FROM Protocol
                        WHERE Module = :mod 
                          AND FormatID = :fid
                          AND IsDefault = {db_manager.sql_bool(True)} 
                          AND IsActive = {db_manager.sql_bool(True)}
                    """), {"mod": module, "fid": file_format_id}).fetchone()
                else:
                    row = conn.execute(text(f"""
                        SELECT ProtocolID
                        FROM Protocol
                        WHERE Module = :mod 
                          AND IsDefault = {db_manager.sql_bool(True)} 
                          AND IsActive = {db_manager.sql_bool(True)}
                    """), {"mod": module}).fetchone()
                
                if not row:
                    if file_format_id is not None:
                        logging.info(f"No default protocol found for module {module} with FormatID {file_format_id}")
                    else:
                        logging.info(f"No default protocol found for module {module}")
                    return None
                
                # Use index access for SQL Server compatibility
                protocol_id = row[0]  # ProtocolID
                protocol = ProtocolManager.load_protocol(protocol_id)
                
                return protocol
                
        except Exception as e:
            logging.error(f"Get default protocol error: {e}", exc_info=True)
            return None
    
    @staticmethod
    def get_protocols(module_filter: Optional[str] = None, file_format_id: Optional[int] = None) -> List[Protocol]:
        """
        Get active protocols, optionally filtered by module and/or file format
        
        Args:
            module_filter: Optional module filter ('LSC', 'SIAM', 'NGAM')
            file_format_id: Optional file format ID filter
            
        Returns:
            List of protocols
        """
        try:
            # Check if database is ready
            if not hasattr(db_manager, '_engine') or db_manager._engine is None:
                logging.info("Database not ready yet, cannot get protocols")
                return []
            
            with db_manager.get_connection() as conn:
                # Build query with optional filters (SQL filtering is MUCH faster!)
                # print(f"mod: {module_filter}, fid: {file_format_id}")
                if module_filter and file_format_id is not None:
                    # Both filters
                    rows = conn.execute(text(f"""
                        SELECT ProtocolID FROM Protocol
                        WHERE Module = :mod 
                          AND FormatID = :fid 
                          AND IsActive = {db_manager.sql_bool(True)}
                        ORDER BY Name
                    """), {"mod": module_filter, "fid": file_format_id}).fetchall()
                elif module_filter:
                    # Module only
                    rows = conn.execute(text(f"""
                        SELECT ProtocolID FROM Protocol
                        WHERE Module = :mod AND IsActive = {db_manager.sql_bool(True)}
                        ORDER BY Name
                    """), {"mod": module_filter}).fetchall()
                elif file_format_id is not None:
                    # FormatID only
                    rows = conn.execute(text(f"""
                        SELECT ProtocolID FROM Protocol
                        WHERE FormatID = :fid AND IsActive = {db_manager.sql_bool(True)}
                        ORDER BY Module, Name
                    """), {"fid": file_format_id}).fetchall()
                else:
                    # No filters
                    rows = conn.execute(text(f"""
                        SELECT ProtocolID FROM Protocol
                        WHERE IsActive = {db_manager.sql_bool(True)}
                        ORDER BY Module, Name
                    """)).fetchall()
                
                protocols = []
                for row in rows:
                    # Use index access for SQL Server compatibility
                    protocol_id = row[0]  # ProtocolID
                    p = ProtocolManager.load_protocol(protocol_id)
                    if p:
                        protocols.append(p)
                
                return protocols
                
        except Exception as e:
            logging.error(f"Get protocols error: {e}", exc_info=True)
            return []
    
    @staticmethod
    def get_format_info(file_format_id: int) -> Optional[Dict[str, Any]]:
        """
        Get instrument info from FileFormat
        
        Returns:
            Dict with keys: format_name, instrument_name, instrument_model, category
        """
        try:
            # Check if database is ready
            if not hasattr(db_manager, '_engine') or db_manager._engine is None:
                logging.info("Database not ready yet, cannot get format info")
                return None
            
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT strFormatName, strInstrumentName, strInstrumentModel, intCategory
                    FROM GUItblFileFormat
                    WHERE lngFormatID = :fid
                """), {"fid": file_format_id}).fetchone()
                
                if row:
                    # Use index access for SQL Server compatibility
                    # SELECT order: strFormatName, strInstrumentName, strInstrumentModel, intCategory
                    try:
                        return {
                            'format_name': row[0],  # strFormatName
                            'instrument_name': row[1] if row[1] else '',  # strInstrumentName
                            'instrument_model': row[2] if row[2] else '',  # strInstrumentModel
                            'category': row[3]  # intCategory
                        }
                    except (IndexError, TypeError) as e:
                        logging.error(f"Could not parse format row: {e}")
                        return None
                return None
                
        except Exception as e:
            logging.error(f"Get format info error: {e}")
            return None
    
    @staticmethod
    def get_file_formats_for_module(module: str) -> List[Dict[str, Any]]:
        """
        Get available file formats for a module
        
        Args:
            module: 'LSC', 'SIAM', or 'NGAM'
            
        Returns:
            List of dicts with format info
        """
        try:
            # Check if database is ready
            if not hasattr(db_manager, '_engine') or db_manager._engine is None:
                logging.info("Database not ready yet, cannot load file formats")
                return []
            
            # Get category for module
            category = ProtocolManager._get_category(module)
            
            if category is None:
                logging.warning(f"Unknown module: {module}")
                return []
            
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(f"""
                    SELECT lngFormatID, strFormatName, strInstrumentName, strInstrumentModel
                    FROM GUItblFileFormat
                    WHERE intCategory = :cat AND IsActive = {db_manager.sql_bool(True)}
                    ORDER BY strFormatName
                """), {"cat": category}).fetchall()
                
                formats = []
                for row in rows:
                    # Use index access for SQL Server compatibility
                    # SELECT order: lngFormatID, strFormatName, strInstrumentName, strInstrumentModel
                    try:
                        formats.append({
                            'id': row[0],  # lngFormatID
                            'name': row[1],  # strFormatName
                            'instrument_name': row[2] if row[2] else '',  # strInstrumentName
                            'instrument_model': row[3] if row[3] else ''  # strInstrumentModel
                        })
                    except (IndexError, TypeError) as e:
                        logging.warning(f"Could not parse format row: {e}, row={row}")
                        continue
                
                logging.info(f"Loaded {len(formats)} file formats for {module}")
                return formats
                
        except Exception as e:
            logging.error(f"Get file formats error: {e}")
            return []