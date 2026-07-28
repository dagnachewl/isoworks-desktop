"""
LSC Parser Base Class

Extracted from trims_lsc_details_gui.py
Date: 2026-02-08 10:52:29
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import pandas as pd
import re
import numpy as np
from typing import Optional, List, Dict


class LSCParserStrategy(ABC):
    """
    Strategy interface all LSC parsers must implement.
    - get_headers(): returns a list of source headers the UI can show/match
    - parse(mapping_dict, count_time_unit): returns a standardized DataFrame
      with columns: Position, VialPos, Cycle, Repeat, CountTime, CPM, QIP
    """
    def __init__(self, filepath: str):
        self.filepath = filepath

    @abstractmethod
    def get_headers(self) -> list:
        ...

    @abstractmethod
    def parse(self, mapping_dict: dict, count_time_unit: int = 1) -> pd.DataFrame:
        ...

    def _ensure_vialpos(self, df: pd.DataFrame, pos_col: str = 'Position') -> pd.DataFrame:
        """
        Creates/normalizes 'VialPos' for display and preserves the input label,
        without deciding run-order semantics. Safe for all formats.
        """
        if pos_col not in df.columns:
            # If no position column, create sequential
            df['Position'] = np.arange(1, len(df) + 1, dtype=int)
            df['VialPos']  = df['Position'].astype(str)
            return df

        s = df[pos_col].astype(str).str.strip()
        df['VialPos'] = s
        return df


    def _assign_positions(
        self,
        df: pd.DataFrame,
        *,
        vial_col: str = 'VialPos',
        mode: str = 'sequence',
        order_by: str | None = None
    ) -> pd.DataFrame:
        """
        Assigns integer 'Position' according to the chosen mode.

        Modes:
        - 'sequence': consecutive grouping (run order blocks)
                        Example: A01,A01,A02,A02,A01  -> 1,1,2,2,3
        - 'label'   : unique label factorization (ignores order)
                        Example: A01,A01,A02,A02,A01  -> 1,1,2,2,1

        order_by:
        Optional column name (e.g., 'EndTime') to sort *stable* BEFORE grouping.
        If None, uses file/read order (recommended for HIDEX List exports).
        """
        if vial_col not in df.columns:
            # Nothing to do; create sequential positions
            df['Position'] = np.arange(1, len(df) + 1, dtype=int)
            return df

        work = df
        # Stable sort if requested (keeps original order for ties)
        if order_by and order_by in df.columns:
            work = df.sort_values(by=[order_by, df.index.name or df.index], kind='mergesort')
        else:
            # preserve original read order
            work = df.copy()

        labels = work[vial_col].astype(str)

        if mode == 'label':
            # Factorize by unique label (old behavior)
            work['Position'] = pd.factorize(labels)[0] + 1

        elif mode == 'sequence':
            # Consecutive block grouping:
            # new block starts whenever label changes from previous row
            # NaNs handled by filling a sentinel that won't compare equal to real labels
            series = labels.fillna('__NA__')
            # new block flag = True where current != previous
            new_block = series.ne(series.shift(1, fill_value='__START__'))
            # cumsum across new_block -> 1,1,2,2,3 ... per sequence
            work['Position'] = new_block.cumsum().astype(int)

        else:
            raise ValueError("Unsupported mode for _assign_positions: use 'sequence' or 'label'")

        # Write back in original index order
        df = work.sort_index()
        return df


    def _standardize_cycles(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Ensures 'Cycle' and 'Repeat' exist and are coherent with run-order Position.

        Rules:
        - If 'Cycle' missing but 'Repeat' present -> Cycle := Repeat; Repeat := 1
        - If 'Cycle' still missing -> generate 1..N within each Position block
        """
        if 'Cycle' not in df.columns and 'Repeat' in df.columns:
            df['Cycle'] = df['Repeat']
            df['Repeat'] = 1

        if 'Cycle' not in df.columns:
            if 'Position' in df.columns:
                df['Cycle'] = df.groupby('Position').cumcount() + 1
            else:
                df['Cycle'] = np.arange(1, len(df) + 1, dtype=int)

        if 'Repeat' not in df.columns:
            df['Repeat'] = 1

        return df
    
    # ---- Common helpers shared by all strategies ----
    def _standardize_positions(self, df: pd.DataFrame, pos_col: str) -> pd.DataFrame:
        """
        Ensures Position (int for DB) and VialPos (string for tray display).
        If the source position is numeric -> Position stays numeric, VialPos=str(Position).
        If alphanumeric (e.g., A01 or 'Tray1-07') -> factorize order into Position, keep raw string in VialPos.
        """
        if pos_col not in df.columns:
            # If no explicit position column, create sequential positions
            df['Position'] = np.arange(1, len(df) + 1, dtype=int)
            df['VialPos']  = df['Position'].astype(str)
            return df

        s = df[pos_col].astype(str).str.strip()
        s_num = pd.to_numeric(s, errors='coerce')
        if s_num.notna().all():
            df['Position'] = s_num.astype(int)
            df['VialPos']  = df['Position'].astype(str)
        else:
            df['VialPos']  = s
            df['Position'] = pd.factorize(s)[0] + 1
        return df
    
    def _apply_time_unit(self, df: pd.DataFrame, count_time_unit: int) -> pd.DataFrame:
        """
        Normalizes CountTime to minutes for the pipeline.
        count_time_unit: 1=Minutes (no change), 2=Seconds (convert /60)
        """
        if 'CountTime' in df.columns and int(count_time_unit) == 2:
            df['CountTime'] = pd.to_numeric(df['CountTime'], errors='coerce').fillna(0.0) / 60.0
        return df

# --- Quantulus canonicalization helper --------------------------------------
_Q_CPM_RE = re.compile(r'^(?:CPM[_\-]?)?(\d+)$', re.I)

