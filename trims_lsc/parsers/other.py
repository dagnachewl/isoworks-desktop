"""
Other LSC Parsers (Packard, Generic)

Extracted from trims_lsc_details_gui.py
Date: 2026-02-08 10:52:29
"""
from __future__ import annotations

import pandas as pd
import numpy as np
import re
from typing import Optional, Dict, List
from .base import LSCParserStrategy


class DelimitedParserStrategy(LSCParserStrategy):
    """
    Generic delimited-file parser supporting Hidex and Packard.
    dialect: 'hidex' | 'packard' | 'generic'
    """
    def __init__(self, filepath: str, dialect: str = 'generic', separator: str = 'auto'):
        super().__init__(filepath)
        self.dialect   = dialect
        self.separator = separator

    def _detect_separator(self) -> str:
        if self.separator != 'auto':
            return self.separator
        try:
            with open(self.filepath, 'r', encoding='utf-8', errors='ignore') as f:
                lines = [f.readline() for _ in range(20)]
                content = "".join(lines)
                return ';' if content.count(';') > content.count(',') else ','
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return ','

    def _find_header_line(self) -> int:
        # Minimal, robust heuristic: look for common tokens
        candidates = {
            'hidex': ['Pos', 'SampleType', 'Time', 'CPM', 'Rpt'],
            'packard': ['S#', 'Count Time', 'CPMA', 'tSIE'],
            'generic': []  # assume first line
        }
        keys = candidates.get(self.dialect, [])
        try:
            with open(self.filepath, 'r', encoding='utf-8', errors='ignore') as f:
                for i, line in enumerate(f):
                    U = line.upper()
                    if not keys or sum(1 for k in keys if k.upper() in U) >= max(1, len(keys)//2):
                        return i
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        return 0

    def get_headers(self) -> list:
        sep = self._detect_separator()
        hdr_idx = self._find_header_line()
        df0 = pd.read_csv(self.filepath, sep=sep, header=hdr_idx, nrows=0, engine='python')
        return [str(c) for c in df0.columns]

    def parse(self, mapping_dict: dict, count_time_unit: int = 1) -> pd.DataFrame:
        """
        mapping_dict: {'Position': 'Pos', 'Repeat': 'Rpt', 'Cycle': 'Cycle', 'CountTime': 'Time', 'CPM': 'CPMroi1', 'QIP': 'QPI', ...}
        """
        sep = self._detect_separator()
        hdr_idx = self._find_header_line()
        df_raw = pd.read_csv(self.filepath, sep=sep, header=hdr_idx, engine='python', on_bad_lines='skip')

        # Build standardized output by mapping target->source
        df = pd.DataFrame()
        for target, source in (mapping_dict or {}).items():
            if source in df_raw.columns:
                df[target] = df_raw[source]

        # Normalize time unit (Seconds->Minutes)
        df = self._apply_time_unit(df, count_time_unit)

        # 1) Always create VialPos from the mapped 'Position' column
        df = self._ensure_vialpos(df, pos_col='Position')
        # 2) Sequence-based run order for HIDEX formats
        if self.dialect == 'hidex':
            # Prefer file order; use EndTime ordering only if your lab requires time-based order
            order_hint = 'EndTime' if 'EndTime' in df.columns else None
            df = self._assign_positions(df, vial_col='VialPos', mode='sequence', order_by=order_hint)
        else:
            # Default (Packard/generic): label-based (factorize) is fine
            df = self._assign_positions(df, vial_col='VialPos', mode='label')
        df = self._standardize_cycles(df)
        
        # --- HIDEX List: Preserve all CPMroiN columns and useful context ---
        if self.dialect == 'hidex':
            # Copy every CPMroiN column into the standardized df (keep names unchanged)
            roi_cols = [c for c in df_raw.columns if re.fullmatch(r'CPMroi\d+', str(c), flags=re.I)]
            for c in sorted(roi_cols, key=lambda x: int(re.findall(r'\d+', x)[0])):
                df[str(c)] = pd.to_numeric(df_raw[c], errors='coerce')
            # Keep useful context if present (won't override canonical fields)
            for aux in ['SQP', 'QIP', 'Pos', 'EndTime', 'Time']:
                if aux in df_raw.columns and aux not in df.columns:
                    df[aux] = df_raw[aux]
        
        # Keep canonical columns
        keep = ['Position','VialPos','Cycle','Repeat','CountTime','CPM','QIP']
        for c in keep:
            if c not in df.columns:
                df[c] = np.nan
        return df #df.dropna(subset=['Position', 'CPM'])


class PackardLSCParser:
    """Complete Packard Parser with Mapping-driven ingestion."""
    def __init__(self, filepath, separator=';'):
        self.filepath = filepath
        self.separator = separator

    def _find_header_line(self) -> int:
        """Finds header line (VBA: InStr(strLine, 'S#'))."""
        with open(self.filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for i, line in enumerate(f):
                if "S#" in line and "Count Time" in line:
                    return i
        return 0

    def get_headers(self):
        """Used by _load_preview for UI mapping."""
        idx = self._find_header_line()
        df = pd.read_csv(self.filepath, sep=self.separator, header=idx, nrows=0)
        return df.columns.tolist()

    def parse(self, mapping_dict) -> pd.DataFrame:
        """
        Parses raw text into standardized DataFrame using UI mapping.
        Replicates VBA iCycleArray(i) = iCycleArray(i) + 1.
        """
        header_idx = self._find_header_line()
        # Read the full data block
        df_raw = pd.read_csv(self.filepath, sep=self.separator, header=header_idx, 
                             engine='python', on_bad_lines='skip')

        # Create standardized output
        df = pd.DataFrame()
        
        # Apply the mapping (e.g., 'CPMA' -> 'CPM', 'S#' -> 'Position')
        for target, source in mapping_dict.items():
            if source in df_raw.columns:
                df[target] = df_raw[source]

        # Ensure Position is numeric (VBA: CDbl(i))
        df['Position'] = pd.to_numeric(df['Position'], errors='coerce')
        df = df.dropna(subset=['Position'])

        # Replicate VBA Cycle logic
        # Instead of an array, we use groupby to count occurrences per Position
        df['Cycle'] = df.groupby('Position').cumcount() + 1
        df['Repeat'] = 1  # Standard default

        # Consistency Check: Ensure numeric data
        for col in ['CPM', 'CountTime', 'QIP']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

        return df
    

class HidexLSCParser:
    """Parser for Hidex LSC CSV data files, adapted from TRIMS logic."""
    def __init__(self, filepath, separator='auto'):
        self.filepath = filepath
        self.separator = separator if separator != 'auto' else self._detect_separator()

    def _detect_separator(self):
        """Auto-detects field separator based on frequency."""
        try:
            with open(self.filepath, 'r', encoding='utf-8', errors='ignore') as f:
                # Read first 20 lines to detect separator
                lines = [f.readline() for _ in range(20)]
                content = "".join(lines)
                return ';' if content.count(';') > content.count(',') else ','
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return ','

    def _find_header_line(self) -> int:
        """
        Finds the 0-indexed line number containing the data header.
        Replicates VBA InStr check for 'Pos' and 'SampleType'.
        """
        key_headers = ['Pos', 'SampleType', 'Time', 'CPM', 'EndTime', 'Rpt']
        try:
            with open(self.filepath, 'r', encoding='utf-8', errors='ignore') as f:
                for line_num, line in enumerate(f):
                    line_upper = line.upper()
                    # Check if line contains essential Hidex headers
                    matches = sum(1 for h in key_headers if h.upper() in line_upper)
                    if matches >= 2:
                        return line_num
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        return 0 # Default to first line if search fails

    def parse(self) -> pd.DataFrame:
        """Parses the CSV into a standardized DataFrame."""
        header_idx = self._find_header_line()
        
        df = pd.read_csv(
            self.filepath, 
            sep=self.separator, 
            header=header_idx, 
            encoding='utf-8', 
            engine='python'
        )

        # Standardize columns to match TRIMS internal pipeline
        rename_map = {
            'Pos': 'Position',
            'Rpt': 'Repeat',
            'Time': 'CountTime',
            'CPMroi1': 'CPM', 
            'QPI': 'QIP',
            'EndTime': 'start_datetime'
        }
        df = df.rename(columns=rename_map)

        # Generate 'Cycle' if missing (Common in Hidex plate exports)
        if 'Cycle' not in df.columns and 'Repeat' in df.columns:
            df['Cycle'] = df['Repeat']
            df['Repeat'] = 1
        is_numeric = pd.to_numeric(df['Position'], errors='coerce').notnull().all()
        if not is_numeric:
            df['VialPos'] = df['Position']
            df['Position'] = pd.factorize(df['VialPos'])[0] + 1
        else:
            df['Position'] = pd.to_numeric(df['Position'], errors='coerce')
            df['VialPos'] = df['Position'].astype(str)
            
        return df
    

