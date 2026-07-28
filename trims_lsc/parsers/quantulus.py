"""
Quantulus LSC Parser Strategies

Extracted from trims_lsc_details_gui.py
Date: 2026-02-08 10:52:29
"""
from __future__ import annotations

import logging
import re
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional, List, Dict, Iterator
from .base import LSCParserStrategy
from datetime import datetime

# ============ Quantulus REGISTRY parser ============
QHEAD_RE = re.compile(r'^(Q\d{6}N\.\d{3})\s+(\d{1,2}\s+\w{3}\s+\d{4})\s+(\d{1,2}:\d{2})\s*$', re.I)
WINDOW_HDR_RE = re.compile(r'^\s*WINDOW\s+CHANNELS\s+MCA\s+HALF', re.I)

@dataclass
class LSCWindow:
    wNumber: int
    channelFrom: int
    channelTo: int
    MCA: int
    Half: int
    label: str

def _parse_minutes(s: str):
    m = re.match(r'^(\d+):(\d+(?:\.\d+)?)$', s.strip())
    if not m:
        return None
    return int(m.group(1)) + float(m.group(2))/60.0

def parse_windows(lines):
    windows = []
    start = None
    for i, ln in enumerate(lines[:200]):
        if WINDOW_HDR_RE.search(ln):
            start = i + 1
            break
    if start is None:
        return windows
    i = start
    while i < len(lines):
        t = lines[i].strip()
        if not t:
            i += 1
            continue
        if re.search(r'^(SELECTED PRINTOUT|SEND SPECTRA|RESOLUTION OF SPECTRA|LISTING|INSTRUMENT NUMBER)', t, re.I):
            break
        m = re.match(r'^(\d+)\s+(\d+)\-\s*(\d+)\s+(\d+)\s+(\d+)\s*$', t)
        if m:
            windows.append(LSCWindow(
                wNumber=int(m.group(1)),
                channelFrom=int(m.group(2)),
                channelTo=int(m.group(3)),
                MCA = int(m.group(4)), 
                Half = int(m.group(5)),
                label=f"F{m.group(1)}: {m.group(2)}-{m.group(3)}-{m.group(4)}{m.group(5)}",
            ))
        i += 1
    if all(w.wNumber != 9 for w in windows):
        windows.append(LSCWindow(9, 1, 1024, 1, 0, 'F9: SQP'))
    return windows

def iter_cycles(lines, instrument_no=None):
    N = len(lines)
    i = 0
    while i < N:
        m = QHEAD_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        meas_file, date_str, time_str = m.group(1), m.group(2), m.group(3)
        try:
            start_iso = datetime.strptime(f"{date_str} {time_str}", "%d %b %Y %H:%M").isoformat()
        except Exception:
            start_iso = None
        block_end = min(i + 6, N)
        block = [lines[j] for j in range(i, block_end)]
        toks1 = block[1].split() if len(block) > 1 else []
        if len(toks1) < 10:
            i += 1
            continue
        cyc, pos, rep = int(toks1[0]), int(toks1[1]), int(toks1[2])
        ctime_s = toks1[3]
        dt2 = float(toks1[5])
        cucnts = float(toks1[6])
        sqp = 0.0 if toks1[7] == ".00" else float(toks1[7])
        sqp_pct = 0.0 if toks1[8] == ".00" else float(toks1[8])
        stime_s = toks1[9]
        ctime_min = _parse_minutes(ctime_s) or 0.0
        net_minutes = ctime_min - (dt2/60.0)
        nums = []
        for k in range(2, min(6, len(block))):
            for token in block[k].split():
                token = ('0' + token) if token.startswith('.') else token
                try:
                    nums.append(float(token))
                except:
                    pass
        triplets = [(nums[j], nums[j+1], nums[j+2]) for j in range(0, min(len(nums), 8*3), 3)]
        counts = [tp[1] for tp in triplets]
        cpms = [round(c / net_minutes, 3) if net_minutes > 0 else 0.0 for c in counts]
        yield {
            'meas_file': meas_file,
            'start_datetime': start_iso,
            'Cycle': cyc,
            'Position': pos,
            'Repeat': rep,
            'CountTime': net_minutes,
            'SQP': sqp,
            'SQP_pct': sqp_pct,
            'stime': stime_s,
            'counts': counts,
            'cpms': cpms,
            'instrument_no': instrument_no,
        }
        i += 6
        
class QuantulusParserStrategy(LSCParserStrategy):
    """
    Parses Quantulus registry files:
    - Only keeps CPM windows where MCA=1 and Half=2, named CPM1, CPM2, ...
    - Removes 'counts' and 'cpms' array columns.
    - Uses SQP as QIP.
    """

    def get_headers(self) -> list:
        return ['CPM_window', 'QIP_window']

    def parse(self, mapping_dict: dict, count_time_unit: int = 1) -> pd.DataFrame:
        with open(self.filepath, 'r', encoding='latin1', errors='ignore') as f:
            lines = f.read().splitlines()

        # Parse windows and filter for MCA=1, Half=2
        all_windows = parse_windows(lines)
        selected_windows = [w for w in all_windows if w.MCA == 1 and w.Half == 2]

        # Parse cycles (your existing helper)
        records = list(iter_cycles(lines, instrument_no=None))
        if not records:
            raise ValueError("No Quantulus cycles found in file (header/format mismatch).")
        df = pd.DataFrame(records)

        # Expand CPMs for selected windows only
        if 'cpms' in df.columns:
            for idx, w in enumerate(selected_windows):
                col_name = f"CPM{idx+1}"
                df[col_name] = df['cpms'].apply(
                    lambda xs: float(xs[idx]) if isinstance(xs, (list, np.ndarray, pd.Series)) and idx < len(xs) else np.nan
                )

        # Remove 'cpms' and 'counts' columns
        for arr_col in ['cpms', 'counts']:
            if arr_col in df.columns:
                del df[arr_col]

        # Canonical CPM: use mapping_dict['CPM_window'] (1-based index)
        win_sel = mapping_dict.get('CPM_window', 1)
        try:
            idx = max(1, int(win_sel)) - 1
            col = f"CPM{idx+1}"
            if col in df.columns:
                df['CPM'] = pd.to_numeric(df[col], errors='coerce')
            else:
                df['CPM'] = np.nan
        except Exception:
            df['CPM'] = np.nan

        # QIP: always use SQP if present
        if 'SQP' in df.columns:
            df['QIP'] = pd.to_numeric(df['SQP'], errors='coerce')
        else:
            df['QIP'] = np.nan

        # Normalise time unit if needed
        df = self._apply_time_unit(df, count_time_unit)
        df = self._ensure_vialpos(df, pos_col='Position')
        df = self._standardize_cycles(df)

        return df

# --- Delimited CSV Strategy (Hidex / Packard / Generic) ---

class _QWin:
    idx: int       # ordinal index (0-based) in the header order
    ch_from: int
    ch_to: int
    mca: int
    half: int


class QuantulusRegistryParserModern(LSCParserStrategy):
    """
    Produces a tidy DataFrame for Quantulus registry:
      Columns (present-only, in canonical display order):
        CYC, POS, REP, CTIME, DTIME1, DTIME2, SQP, CPM1..CPMn
      Rules:
        - Keep CPM windows where MCA==1 and Half==2 (in header order)
        - Drop COUNTS and percentages; no 'cpms'/'counts' list columns
        - Attach UI labels per CPM in df.attrs['quantulus_cpm_labels'] and a display->real map
    """

    _re_filehdr = re.compile(
        r'^\s*Q\d{6}N\.\d{3}\s+\d{1,2}\s+\w{3}\s+\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*$',
        re.I
    )

    def __init__(self, filepath: str):
        super().__init__(filepath)

    @staticmethod
    def _to_float(s: str) -> float:
        try:
            return float(s.replace(',', ''))
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return 0.0

    def _parse_meta_line(self, s: str) -> dict:
        # Expected (printed meta): CYC POS REP CTIME DTIME1 DTIME2 CUCNTS SQP SQP% STIME
        toks = s.split()
        if len(toks) < 10:
            raise ValueError(f"Malformed Quantulus meta line: {s!r}")
        return {
            'CYC':    int(float(toks[0])),
            'POS':    int(float(toks[1])),
            'REP':    int(float(toks[2])),
            'CTIME':  toks[3],                 # keep as e.g. '20:01.778'
            'DTIME1': self._to_float(toks[4]),
            'DTIME2': self._to_float(toks[5]),
            'SQP':    self._to_float(toks[7]),
        }

    def _parse_cpm_block(self, lines: list[str], start: int) -> tuple[list[float], int]:
        """
        Collect up to 8 CPMs in header order, ignoring COUNTS and percentages.
        Each line usually holds two (CPM, COUNTS, CPM%) triples -> take CPM at positions 0 and 3.
        """
        cpms_all, i = [], start
        while i < len(lines) and len(cpms_all) < 8:
            s = lines[i].strip()
            if not s:
                i += 1; continue
            nums = re.findall(r'[+\-]?(?:\d+\.\d+|\d+|\.\d+)', s)
            if len(nums) < 3:  # not a CPM triple row
                break
            try:
                cpms_all.append(float(nums[0]))
                if len(nums) >= 6 and len(cpms_all) < 8:
                    cpms_all.append(float(nums[3]))
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            i += 1
        
        return cpms_all, i

    # --- in class QuantulusRegistryParserModern (near its other methods) ---
    def get_headers(self) -> list:
        """
        Returns canonical, user-facing header choices for mapping:
        - CPM1..CPM8 (window CPMs)
        - SQP (Quantulus QIP)
        """
        try:
            with open(self.filepath, 'r', encoding='latin1', errors='ignore') as f:
                lines = f.read().splitlines()
            wins = parse_windows(lines)  # already available in file
            # Offer CPM1..CPM8 in order, only up to the detected windows (fallback 8)
            max_c = min(8, max([int(getattr(w, 'wNumber', 0) or 0) for w in wins] + [8]))
            cpm_cols = [f"CPM{i}" for i in range(1, max_c + 1)]
            return cpm_cols + ["SQP"]
        except Exception:
            # Safe fallback if headers can't be parsed
            return [f"CPM{i}" for i in range(1, 9)] + ["SQP"]

    def parse(self, mapping_dict: dict | None = None, count_time_unit: int = 1) -> pd.DataFrame:
      
        with open(self.filepath, 'r', encoding='latin1', errors='ignore') as f:
            lines = f.read().splitlines()

        wins = parse_windows(lines)           # full window list from header
        # Build a quick lookup: window number -> "[from–to]"
        win_by_no = {int(w.wNumber): f"[{int(w.channelFrom)}–{int(w.channelTo)}]" for w in wins if getattr(w, 'wNumber', None) is not None}
        # Scan record blocks by file headers
        out_rows = []
        i, N = 0, len(lines)
        while i < N:
            if not self._re_filehdr.match(lines[i]):
                i += 1
                continue

            # meta line (next non-empty)
            j = i + 1
            while j < N and not lines[j].strip():
                j += 1
            if j >= N:
                break

            meta = self._parse_meta_line(lines[j])
            # parse CPM/COUNTS lines: returns [cpm1, cpm2, ...]
            cpms_all, k = self._parse_cpm_block(lines, j + 1)
            row = {**meta}
            # Materialize CPM1..CPM8 (or fewer if cpms_all is shorter)
            for c_idx in range(8):
                col = f"CPM{c_idx+1}"
                row[col] = float(cpms_all[c_idx]) if c_idx < len(cpms_all) else float('nan')

            out_rows.append(row)
            i = k

        df = pd.DataFrame(out_rows)
        if df.empty:
            return df  # let caller surface the "no cycles" message

        # Column order for display (if present)
        base = ['CYC', 'POS', 'REP', 'CTIME', 'DTIME1', 'DTIME2', 'SQP']
        cpm_cols = [c for c in df.columns if re.fullmatch(r'CPM\d+', c)]
        cpm_cols = sorted(cpm_cols, key=lambda x: int(re.findall(r'\d+', x)[0]))
        present = [c for c in base + cpm_cols if c in df.columns]
        if present:
            df = df[present]

        # Attach labels: try to label CPMn with Window n; fall back to plain "CPMn"
        labels = {}
        display_to_real = {}
        for n in range(1, 9):
            col = f"CPM{n}"
            if col in df.columns:
                rng = win_by_no.get(n)  # only if header had a window "n"
                disp = f"{col}: {rng}" if rng else col
                labels[col] = disp
                display_to_real[disp] = col
        df.attrs['quantulus_cpm_labels'] = labels
        df.attrs['quantulus_cpm_label_map'] = display_to_real

        # Apply mapping (expecting canonical tokens 'CPM#' & 'SQP')
        m = mapping_dict or {}
        cpm_choice = m.get('CPM')
        if cpm_choice and cpm_choice in df.columns:
            df['CPM'] = pd.to_numeric(df[cpm_choice], errors='coerce')
        elif 'CPM1' in df.columns:
            df['CPM'] = pd.to_numeric(df['CPM1'], errors='coerce')
        else:
            df['CPM'] = float('nan')
        df['QIP'] = pd.to_numeric(df.get('SQP', float('nan')), errors='coerce')        

        # 1) Canonical TRIMS columns from registry meta
        #    Position ← POS, Cycle ← CYC, Repeat ← REP
        df['Position'] = pd.to_numeric(df.get('POS'), errors='coerce')
        df['Cycle']    = pd.to_numeric(df.get('CYC'), errors='coerce')
        df['Repeat']   = pd.to_numeric(df.get('REP'), errors='coerce')

        # 2) VialPos for display (string version of Position)
        df['VialPos'] = df['Position'].astype('Int64').astype(str)

        # 3) CountTime in minutes (same logic as legacy iterator)
        #    CTIME like '20:01.778' -> minutes; DTIME2 is seconds to subtract
        def _to_minutes(s):
            try:
                # reuse the module-level _parse_minutes(s) already in this file
                return _parse_minutes(str(s)) or 0.0
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); return 0.0

        ctime_min  = df['CTIME'].apply(_to_minutes)
        dt2_sec    = pd.to_numeric(df.get('DTIME2'), errors='coerce').fillna(0.0)
        df['CountTime'] = (ctime_min - (dt2_sec / 60.0)).astype(float)

        # 4) Keep canonical first, then everything else for transparency
        canonical = ['Position','VialPos','Cycle','Repeat','CountTime','CPM','QIP']
        front = [c for c in canonical if c in df.columns]
        rest  = [c for c in df.columns if c not in front]
        df = df[front + rest]        
        # DBG(f"DEBUGQuantulus: final_rows={len(df)} final_cols={list(df.columns)}")

        return df


