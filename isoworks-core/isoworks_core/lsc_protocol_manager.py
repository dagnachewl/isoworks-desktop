"""
LSC Protocol Manager
Handles loading, saving, and applying LSC analysis protocols
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass, asdict
from sqlalchemy import text

from db_core import db_manager


@dataclass
class ColumnMapping:
    """Represents a single column mapping in a protocol"""
    target_field: str
    source_header: str
    is_net: bool = False
    requires_background: bool = True
    uncertainty_column: Optional[str] = None
    transform_formula: Optional[str] = None
    display_order: int = 0
    notes: Optional[str] = None

@dataclass
class ProtocolSettings:
    """Calculation and processing settings for a protocol"""
    # Signal Processing
    signal_metric: str = 'CPM'  # 'CPM' or 'DPM'
    efficiency_source: str = 'Per-run (computed)'
    
    # Outlier Detection
    outlier_method: str = 'Chauvenet'
    outlier_threshold: float = 2.0
    outlier_iterations: int = 3
    outlier_apply_to: str = 'CPM'
    
    # Activity Calculation
    activity_unit: int = 1  # 1=TU, 2=DPM/kg, 3=Bq/kg, 4=pCi/L
    enrichment_factor_method: int = 2  # 0=None, 1=Deuterium, 2=Spike, 4=Manual
    
    # Background Handling
    background_mode: str = 'Compute'  # 'UseFile', 'Compute', 'Manual'
    background_value: Optional[float] = None
    
    # QC Thresholds
    min_count_time: float = 30.0
    max_rsd: float = 5.0
    require_qip_check: bool = True


@dataclass
class LSCProtocol:
    """Complete LSC analysis protocol"""
    protocol_id: Optional[int] = None
    protocol_name: str = ""
    isotope_id: int = 200  # H-3
    file_format_id: int = 6  # HIDEX Matrix
    description: str = ""
    is_default: bool = False
    is_active: bool = True
    
    mappings: List[ColumnMapping] = None
    settings: ProtocolSettings = None
    
    created_by: Optional[str] = None
    created_date: Optional[datetime] = None
    modified_by: Optional[str] = None
    modified_date: Optional[datetime] = None
    
    def __post_init__(self):
        if self.mappings is None:
            self.mappings = []
        if self.settings is None:
            self.settings = ProtocolSettings()
    
    def to_dict(self) -> dict:
        """Convert protocol to dictionary for JSON serialization"""
        return {
            'protocol_id': self.protocol_id,
            'protocol_name': self.protocol_name,
            'isotope_id': self.isotope_id,
            'file_format_id': self.file_format_id,
            'description': self.description,
            'is_default': self.is_default,
            'mappings': [asdict(m) for m in self.mappings],
            'settings': asdict(self.settings),
            'created_by': self.created_by,
            'created_date': self.created_date.isoformat() if self.created_date else None,
            'modified_by': self.modified_by,
            'modified_date': self.modified_date.isoformat() if self.modified_date else None,
        }
    
    def to_json(self) -> str:
        """Serialize protocol to JSON string"""
        return json.dumps(self.to_dict(), indent=2)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'LSCProtocol':
        """Create protocol from dictionary"""
        mappings = [ColumnMapping(**m) for m in data.get('mappings', [])]
        settings = ProtocolSettings(**data.get('settings', {}))
        
        # Remove mappings and settings from data before creating protocol
        protocol_data = {k: v for k, v in data.items() if k not in ('mappings', 'settings')}
        protocol = cls(**protocol_data)
        protocol.mappings = mappings
        protocol.settings = settings
        return protocol
    
    @classmethod
    def from_json(cls, json_str: str) -> 'LSCProtocol':
        """Deserialize protocol from JSON string"""
        return cls.from_dict(json.loads(json_str))


class LSCProtocolManager:
    """Manages LSC protocol CRUD operations"""
    
    @staticmethod
    def get_default_protocol(isotope_id: int, file_format_id: int) -> Optional[LSCProtocol]:
        """Get the default protocol for an isotope-format combination"""
        with db_manager.get_connection() as conn:
            row = conn.execute(text(f"""
                SELECT ProtocolID FROM TRIMS.LSCProtocol
                WHERE IsotopeID = :iso AND FileFormatID = :fmt AND IsDefault = {db_manager.sql_bool(True)} AND IsActive = {db_manager.sql_bool(True)}
            """), {'iso': isotope_id, 'fmt': file_format_id}).fetchone()
            
            if row:
                return LSCProtocolManager.load_protocol(row.ProtocolID)
        return None
    
    @staticmethod
    def load_protocol(protocol_id: int) -> Optional[LSCProtocol]:
        """Load a complete protocol by ID"""
        with db_manager.get_connection() as conn:
            # Load protocol header
            p_row = conn.execute(text("""
                SELECT ProtocolID, ProtocolName, IsotopeID, FileFormatID,
                       Description, IsDefault, IsActive, CreatedBy, CreatedDate,
                       ModifiedBy, ModifiedDate
                FROM TRIMS.LSCProtocol
                WHERE ProtocolID = :pid
            """), {'pid': protocol_id}).fetchone()
            
            if not p_row:
                return None
            
            protocol = LSCProtocol(
                protocol_id=p_row.ProtocolID,
                protocol_name=p_row.ProtocolName,
                isotope_id=p_row.IsotopeID,
                file_format_id=p_row.FileFormatID,
                description=p_row.Description or "",
                is_default=bool(p_row.IsDefault),
                is_active=bool(p_row.IsActive),
                created_by=p_row.CreatedBy,
                created_date=p_row.CreatedDate,
                modified_by=p_row.ModifiedBy,
                modified_date=p_row.ModifiedDate
            )
            
            # Load mappings
            m_rows = conn.execute(text("""
                SELECT TargetField, SourceHeader, IsNet, RequiresBackground,
                       UncertaintyColumn, TransformFormula, DisplayOrder, Notes
                FROM TRIMS.LSCProtocolMapping
                WHERE ProtocolID = :pid
                ORDER BY DisplayOrder
            """), {'pid': protocol_id}).fetchall()
            
            protocol.mappings = [
                ColumnMapping(
                    target_field=m.TargetField,
                    source_header=m.SourceHeader or "",
                    is_net=bool(m.IsNet),
                    requires_background=bool(m.RequiresBackground),
                    uncertainty_column=m.UncertaintyColumn,
                    transform_formula=m.TransformFormula,
                    display_order=m.DisplayOrder or 0,
                    notes=m.Notes
                )
                for m in m_rows
            ]
            
            # Load settings
            s_row = conn.execute(text("""
                SELECT SignalMetric, EfficiencySource, OutlierMethod, OutlierThreshold,
                       OutlierIterations, OutlierApplyTo, ActivityUnit, EnrichmentFactorMethod,
                       BackgroundMode, BackgroundValue, MinCountTime, MaxRSD, RequireQIPCheck
                FROM TRIMS.LSCProtocolSettings
                WHERE ProtocolID = :pid
            """), {'pid': protocol_id}).fetchone()
            
            if s_row:
                protocol.settings = ProtocolSettings(
                    signal_metric=s_row.SignalMetric or 'CPM',
                    efficiency_source=s_row.EfficiencySource or 'Per-run (computed)',
                    outlier_method=s_row.OutlierMethod or 'Chauvenet',
                    outlier_threshold=float(s_row.OutlierThreshold or 2.0),
                    outlier_iterations=int(s_row.OutlierIterations or 3),
                    outlier_apply_to=s_row.OutlierApplyTo or 'CPM',
                    activity_unit=int(s_row.ActivityUnit or 1),
                    enrichment_factor_method=int(s_row.EnrichmentFactorMethod or 2),
                    background_mode=s_row.BackgroundMode or 'Compute',
                    background_value=float(s_row.BackgroundValue) if s_row.BackgroundValue else None,
                    min_count_time=float(s_row.MinCountTime or 30.0),
                    max_rsd=float(s_row.MaxRSD or 5.0),
                    require_qip_check=bool(s_row.RequireQIPCheck)
                )
            
            return protocol
    
    @staticmethod
    def save_protocol(protocol: LSCProtocol, user: str = 'SYSTEM') -> int:
        """Save or update a protocol. Returns protocol_id"""
        with db_manager.get_connection() as conn:
            now_func = "NOW()" if db_manager.dialect == "POSTGRESQL" else "GETDATE()"
            if protocol.protocol_id:
                # Update existing
                conn.execute(text(f"""
                    UPDATE TRIMS.LSCProtocol
                    SET ProtocolName = :name, Description = :desc, IsDefault = :def,
                        IsActive = :active, ModifiedBy = :user, ModifiedDate = {now_func}
                    WHERE ProtocolID = :pid
                """), {
                    'pid': protocol.protocol_id,
                    'name': protocol.protocol_name,
                    'desc': protocol.description,
                    'def': bool(protocol.is_default),
                    'active': bool(protocol.is_active),
                    'user': user
                })
                protocol_id = protocol.protocol_id
                
                # Delete old mappings and settings
                conn.execute(text("DELETE FROM TRIMS.LSCProtocolMapping WHERE ProtocolID = :pid"),
                           {'pid': protocol_id})
                conn.execute(text("DELETE FROM TRIMS.LSCProtocolSettings WHERE ProtocolID = :pid"),
                           {'pid': protocol_id})
            else:
                # Insert new
                if db_manager.dialect == "POSTGRESQL":
                    insert_lsc_sql = """
                        INSERT INTO TRIMS.LSCProtocol
                            (ProtocolName, IsotopeID, FileFormatID, Description, IsDefault, IsActive, CreatedBy)
                        VALUES (:name, :iso, :fmt, :desc, :def, :active, :user)
                        RETURNING ProtocolID
                    """
                else:
                    insert_lsc_sql = """
                        INSERT INTO TRIMS.LSCProtocol
                            (ProtocolName, IsotopeID, FileFormatID, Description, IsDefault, IsActive, CreatedBy)
                        OUTPUT INSERTED.ProtocolID
                        VALUES (:name, :iso, :fmt, :desc, :def, :active, :user)
                    """
                result = conn.execute(text(insert_lsc_sql), {
                    'name': protocol.protocol_name,
                    'iso': protocol.isotope_id,
                    'fmt': protocol.file_format_id,
                    'desc': protocol.description,
                    'def': bool(protocol.is_default),
                    'active': bool(protocol.is_active),
                    'user': user
                })
                protocol_id = result.scalar()
                protocol.protocol_id = protocol_id
            
            # Insert mappings
            for mapping in protocol.mappings:
                conn.execute(text("""
                    INSERT INTO TRIMS.LSCProtocolMapping
                        (ProtocolID, TargetField, SourceHeader, IsNet, RequiresBackground,
                         UncertaintyColumn, TransformFormula, DisplayOrder, Notes)
                    VALUES (:pid, :tgt, :src, :net, :bkg, :unc, :formula, :ord, :notes)
                """), {
                    'pid': protocol_id,
                    'tgt': mapping.target_field,
                    'src': mapping.source_header,
                    'net': int(mapping.is_net),
                    'bkg': int(mapping.requires_background),
                    'unc': mapping.uncertainty_column,
                    'formula': mapping.transform_formula,
                    'ord': mapping.display_order,
                    'notes': mapping.notes
                })
            
            # Insert settings
            s = protocol.settings
            conn.execute(text("""
                INSERT INTO TRIMS.LSCProtocolSettings
                    (ProtocolID, SignalMetric, EfficiencySource, OutlierMethod, OutlierThreshold,
                     OutlierIterations, OutlierApplyTo, ActivityUnit, EnrichmentFactorMethod,
                     BackgroundMode, BackgroundValue, MinCountTime, MaxRSD, RequireQIPCheck)
                VALUES (:pid, :sig, :eff, :om, :ot, :oi, :oa, :au, :efm, :bm, :bv, :mct, :mrsd, :qip)
            """), {
                'pid': protocol_id,
                'sig': s.signal_metric,
                'eff': s.efficiency_source,
                'om': s.outlier_method,
                'ot': s.outlier_threshold,
                'oi': s.outlier_iterations,
                'oa': s.outlier_apply_to,
                'au': s.activity_unit,
                'efm': s.enrichment_factor_method,
                'bm': s.background_mode,
                'bv': s.background_value,
                'mct': s.min_count_time,
                'mrsd': s.max_rsd,
                'qip': int(s.require_qip_check)
            })
            
            conn.commit()
            return protocol_id
    
    @staticmethod
    def list_protocols(isotope_id: Optional[int] = None, 
                      file_format_id: Optional[int] = None,
                      active_only: bool = True) -> List[Tuple[int, str, str]]:
        """List available protocols. Returns [(protocol_id, name, description)]"""
        with db_manager.get_connection() as conn:
            sql = "SELECT ProtocolID, ProtocolName, Description FROM TRIMS.LSCProtocol WHERE 1=1"
            params = {}
            
            if isotope_id is not None:
                sql += " AND IsotopeID = :iso"
                params['iso'] = isotope_id
            
            if file_format_id is not None:
                sql += " AND FileFormatID = :fmt"
                params['fmt'] = file_format_id
            
            if active_only:
                sql += f" AND IsActive = {db_manager.sql_bool(True)}"
            
            sql += " ORDER BY IsDefault DESC, ProtocolName"
            
            rows = conn.execute(text(sql), params).fetchall()
            return [(r.ProtocolID, r.ProtocolName, r.Description or "") for r in rows]
    
    @staticmethod
    def save_run_protocol_snapshot(run_id: int, protocol: LSCProtocol, 
                                   was_modified: bool = False, user: str = 'SYSTEM'):
        """Save protocol snapshot for audit trail"""
        with db_manager.get_connection() as conn:
            # Delete existing snapshot for this run
            conn.execute(text("DELETE FROM TRIMS.LSCRunProtocol WHERE RunID = :rid"),
                       {'rid': run_id})
            
            # Insert new snapshot
            conn.execute(text("""
                INSERT INTO TRIMS.LSCRunProtocol
                    (RunID, ProtocolID, ProtocolSnapshot, WasModified, AppliedBy)
                VALUES (:rid, :pid, :snap, :mod, :user)
            """), {
                'rid': run_id,
                'pid': protocol.protocol_id,
                'snap': protocol.to_json(),
                'mod': int(was_modified),
                'user': user
            })
            
            conn.commit()
    
    @staticmethod
    def get_run_protocol_snapshot(run_id: int) -> Optional[LSCProtocol]:
        """Retrieve the protocol used for a specific run"""
        with db_manager.get_connection() as conn:
            row = conn.execute(text("""
                SELECT ProtocolSnapshot FROM TRIMS.LSCRunProtocol
                WHERE RunID = :rid
            """), {'rid': run_id}).fetchone()
            
            if row and row.ProtocolSnapshot:
                return LSCProtocol.from_json(row.ProtocolSnapshot)
        return None
    
    @staticmethod
    def get_mapping_dict(protocol: LSCProtocol) -> Dict[str, str]:
        """Convert protocol mappings to simple dict for parser compatibility"""
        mapping = {}
        for m in protocol.mappings:
            mapping[m.target_field] = m.source_header
            if m.is_net:
                mapping[f'{m.target_field}_is_net'] = True
            if m.uncertainty_column:
                mapping[f'{m.target_field}_unc'] = m.uncertainty_column
        return mapping
    
    @staticmethod
    def create_hidex_matrix_protocol(isotope_id: int = 200) -> LSCProtocol:
        """Factory method to create default HIDEX Matrix protocol"""
        protocol = LSCProtocol(
            protocol_name=f"HIDEX Matrix H-3 Default",
            isotope_id=isotope_id,
            file_format_id=6,
            description="Uses MikroWin's pre-calculated net values",
            is_default=True
        )
        
        # HIDEX Matrix provides NET values - don't recompute
        protocol.mappings = [
            ColumnMapping('CPM', 'CPM H-3 (cpm fit)', is_net=True, requires_background=False,
                        uncertainty_column='Unc.CPM H-3', display_order=1),
            ColumnMapping('DPM', 'DPM', is_net=True, requires_background=False,
                        uncertainty_column='Unc.DPM', display_order=2),
            ColumnMapping('QIP', 'QPE', display_order=3),
            ColumnMapping('Efficiency', 'Eff %', display_order=4),
            ColumnMapping('Background', 'MeanCPM Bg fit', display_order=5),
        ]
        
        protocol.settings = ProtocolSettings(
            signal_metric='CPM',
            efficiency_source='Per-run (file)',
            background_mode='UseFile',
            activity_unit=1,
            enrichment_factor_method=2
        )
        
        return protocol


if __name__ == '__main__':
    # Test protocol creation and serialization
    protocol = LSCProtocolManager.create_hidex_matrix_protocol()
    print("Created HIDEX Matrix Protocol:")
    print(protocol.to_json())
    
    # Test save and load
    try:
        pid = LSCProtocolManager.save_protocol(protocol)
        print(f"\nSaved protocol with ID: {pid}")
        
        loaded = LSCProtocolManager.load_protocol(pid)
        print(f"\nLoaded protocol: {loaded.protocol_name}")
        print(f"Mappings: {len(loaded.mappings)}")
        print(f"Settings: {loaded.settings.signal_metric}")
        
    except Exception as e:
        print(f"Database test failed (expected if not connected): {e}")
