"""
HIDEX Matrix LSC Parser

Extracted from trims_lsc_details_gui.py
Date: 2026-02-08 10:52:29
"""
from __future__ import annotations

import pandas as pd
import numpy as np
import re
from typing import Optional, Dict, List
from .base import LSCParserStrategy


class HidexMatrixParserStrategy(LSCParserStrategy):
    """
    Parser for HIDEX Matrix (ME) report files (text). Mirrors the intent of:
      - ImportLSCRunFromHidex_ME
      - ImportHidexMeanDataToLSCRunMean

    Canonical columns returned:
      Position, VialPos, Cycle, Repeat, CountTime (min), CPM (net CPM fit), QIP (QPE)

    Extra columns preserved (for preview/results and DB save):
      MeanCPM_Bg, FactorF, MeanCPM_Gross, MeanCPM_BgFit, Eff_pct, NetCPM_fit,
      DPM, MeasStart, Cycles, LabID, NetCPM_unc, DPM_unc, ChemiCPM, SampleName,
      QPE, POS, InstrSN, MeasEnd, NetCountTime_min
    """

    _PARAMS = [
        "Mean H-3", "MeanCPM", "CPM H-3 (cpm fit)", "Unc.CPM H-3",
        "MeanCPM Bg", "MeanCPM Bg fit", "Eff %", "F",
        "DPM", "Unc.DPM",
        "Meas. start time", "Meas. end time",
        "Number of cycles", "Net count time/min",
        # optional / sometimes blank metadata
        "Lab ID", "MA21", "Sample name", "CV Alert", "QPE", "POS", "Instr. s/n"
    ]


    def __init__(self, filepath: str, delimiter: str = 'auto'):
        super().__init__(filepath)
        self.delimiter = delimiter

    # ---------- small utilities ----------
    def _detect_delim(self, text: str) -> str | None:
        if self.delimiter != 'auto':
            return self.delimiter
        sc, cm, tb = text.count(';'), text.count(','), text.count('\t')
        if sc >= cm and sc >= tb: return ';'
        if cm >= sc and cm >= tb: return ','
        return None  # whitespace split

    def _is_hdr(self, s: str, k: str) -> bool:
        return k.lower() in (s or "").lower()

    def _tokens_num(self, s: str, delim: str | None) -> list[str]:
        """
        Tokenize numbers while preserving layout. Returns only numeric tokens
        (blanks handled later by right-padding to number of positions).
        Accepts decimal commas and scientific notation.
        """
        parts = (s.strip().split(delim) if delim else s.strip().split())
        out = []
        for tk in parts:
            tk = tk.strip()
            # normalize decimal comma to dot if needed
            tknorm = tk.replace('.', '').replace(',', '.') if (',' in tk and '.' not in tk) else tk
            if tknorm and re.fullmatch(r'[+\-]?(?:\d+\.?\d*|\.\d+)(?:eE[+\-]?\d+)?', tknorm):
                out.append(tknorm)
        return out

    def _collect_numeric_vector(self, lines: list[str], idx: int, delim: str | None, need: int) -> list[float]:
        vals, j, N = [], idx + 1, len(lines)
        while j < N and len(vals) < need:
            s = linesj.strip()
            if not s:
                j += 1; continue
            # stop if we hit another parameter header line (heuristic)
            if any(self._is_hdr(s, p) for p in self._PARAMS):
                break
            for tk in self._tokens_num(s, delim):
                try: vals.append(float(tk))
                except: pass
            j += 1
        if len(vals) < need: vals += [float('nan')] * (need - len(vals))
        return vals[:need]

    def _collect_string_vector(self, lines: list[str], idx: int, delim: str | None, need: int) -> list[str]:
        vals, j, N = [], idx + 1, len(lines)
        while j < N and len(vals) < need:
            s = linesj.strip()
            if not s:
                j += 1; continue
            parts = (s.split(delim) if delim else s.split())
            vals.extend([p.strip() for p in parts if p.strip()])
            j += 1
        if len(vals) < need: vals += [''] * (need - len(vals))
        return vals[:need]

    def _find_param(self, lines: list[str], key: str) -> int | None:
        for i, s in enumerate(lines):
            if self._is_hdr(s, key): return i
        return None

    # ---------- strategy API ----------
    # def get_headers(self) -> list:
    #     return list(self._PARAMS)

    def get_headers(self) -> list:
        """
        Dynamically extract parameter headers from a Hidex Matrix report.
        We treat a line as a header if the next non-empty line looks like a
        numeric data row (CSV/whitespace numbers and/or blanks). On any issue
        or if nothing is detected, we fall back to the static _PARAMS list.
        """
        headers = []
        try:
            with open(self.filepath, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.read().splitlines()

            def _numericish_tokens(line: str) -> bool:
                s = (line or "").strip()
                if not s:
                    return False
                # Tokenize by common separators; tolerate decimal comma, dot, sci. notation
                parts = re.split(r'[,\t; ]+', s)
                if not parts:
                    return False
                n_ok = 0
                for tk in parts:
                    t = tk.strip()
                    if t == "":
                        n_ok += 1
                        continue
                    t_norm = t.replace(',', '.') if (',' in t and '.' not in t) else t
                    if re.fullmatch(r'[+\-]?(?:\d+\.?\d*|\.\d+)(?:eE[+\-]?\d+)?', t_norm):
                        n_ok += 1
                # If at least half the tokens are numeric/blanks, consider it numeric
                return n_ok >= max(1, len(parts) // 2)

            N = len(lines)
            i = 0
            while i < N:
                s = (linesi or "").rstrip()
                i += 1
                if not s:
                    continue
                # Skip data-looking lines at header positions
                if _numericish_tokens(s):
                    continue
                # Look ahead to the next non-empty line; if it is numericish -> 's' is a header
                j = i
                while j < N and not (linesj or "").strip():
                    j += 1
                if j < N and _numericish_tokens(linesj):
                    headers.append(s.strip())

            # Fallback to static list if nothing was detected
            return headers if headers else list(self._PARAMS)
        except Exception:
            # Robust fallback on any IO/parse error
            return list(self._PARAMS)
    
    def _extract_pos_tokens(self, lines: list[str], delim: str | None) -> list[str]:
        """
        Returns a flat list of all POS tokens (e.g., A01, B01, ...).
        """
        idx = self._find_param(lines, "POS")
        if idx is None:
            return []
        tokens = []
        for k in range(1, 8):
            if idx + k >= len(lines):
                break
            s = lines[idx + k].rstrip()
            if not s:
                continue
            parts = s.split(delim) if delim else s.split()
            # Accept empty cells (keep as '')
            for p in parts:
                if p.strip() != "":
                    tokens.append(p.strip())
        return tokens

    def parse(self, mapping_dict: dict, count_time_unit: int = 1) -> pd.DataFrame:
        with open(self.filepath, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.read().splitlines()
        text = "\n".join(lines)
        delim = self._detect_delim(text)

        # (1) infer number of positions from any "dense" aggregate block
        pos_tokens = self._extract_pos_tokens(lines, delim)
        if pos_tokens:
            num_positions = len(pos_tokens)
        if not num_positions or num_positions < 1:
            raise RuntimeError("Hidex Matrix: could not detect number of positions from report")

        # helpers
        def col_num(name: str) -> list[float]:
            j = self._find_param(lines, name)
            if j is None:
                return [float('nan')] * num_positions
            return self._collect_numeric_vector(lines, j, delim, num_positions)

        def col_str(name: str) -> list[str]:
            j = self._find_param(lines, name)
            if j is None:
                return [''] * num_positions
            return self._collect_string_vector(lines, j, delim, num_positions)

        # (2) collect aggregates
        MeanH3        = col_num("Mean H-3")
        MeanCPM_Gross = col_num("MeanCPM")
        NetCPM_fit    = col_num("CPM H-3 (cpm fit)")
        NetCPM_unc    = col_num("Unc.CPM H-3")
        MeanCPM_Bg    = col_num("MeanCPM Bg")
        MeanCPM_BgFit = col_num("MeanCPM Bg fit")
        Eff_pct       = col_num("Eff %")
        F_quench      = col_num("F")
        DPM           = col_num("DPM")
        DPM_unc       = col_num("Unc.DPM")
        QPE           = col_num("QPE")
        POS           = col_num("POS")
        Cycles        = col_num("Number of cycles")
        NetCT_min     = col_num("Net count time/min")
        MeasStart     = col_str("Meas. start time")
        MeasEnd       = col_str("Meas. end time")
        LabID         = col_str("Lab ID")
        MA21          = col_str("MA21")
        SampleName    = col_str("Sample name")
        InstrSN       = col_str("Instr. s/n")

        df = pd.DataFrame({
            'Position'       : np.arange(1, num_positions + 1, dtype=int),
            'VialPos'        : [str(int(v)) if not pd.isna(v) else str(i+1) for i, v in enumerate(POS)],
            'Cycle'          : [int(v) if not pd.isna(v) else 1 for v in Cycles],
            'Repeat'         : 1,
            'CountTime'      : pd.to_numeric(NetCT_min, errors='coerce'),
            'CPM'            : pd.to_numeric(np.where(np.isnan(NetCPM_fit), MeanH3, NetCPM_fit), errors='coerce'),
            'QIP'            : pd.to_numeric(QPE, errors='coerce'),
            # extras kept for saving/preview
            'MeanCPM_Bg'     : pd.to_numeric(MeanCPM_Bg, errors='coerce'),
            'MeanCPM_BgFit'  : pd.to_numeric(MeanCPM_BgFit, errors='coerce'),
            'MeanCPM_Gross'  : pd.to_numeric(MeanCPM_Gross, errors='coerce'),
            'Eff_pct'        : pd.to_numeric(Eff_pct, errors='coerce'),
            'NetCPM_fit'     : pd.to_numeric(NetCPM_fit, errors='coerce'),
            'MeanH3'         : pd.to_numeric(MeanH3, errors='coerce'),
            'NetCPM_unc'     : pd.to_numeric(NetCPM_unc, errors='coerce'),
            'DPM'            : pd.to_numeric(DPM, errors='coerce'),
            'DPM_unc'        : pd.to_numeric(DPM_unc, errors='coerce'),
            'LabID'          : LabID,
            'MA21'           : MA21,
            'SampleName'     : SampleName,
            'InstrSN'        : InstrSN,
            'MeasStart'      : (MeasStart0 if MeasStart else ''),  # initial; we chain below
            'MeasEnd'        : MeasEnd,
        })

        # (4) time chaining: start(1) from file; start(i>1) := end(i-1)
        try:
            if 'MeasEnd' in df.columns:
                ends = df['MeasEnd'].astype(str).tolist()
                starts = [''] * len(ends)
                if isinstance(df.at[0, 'MeasStart'], str) and df.at[0, 'MeasStart']:
                    starts0 = df.at[0, 'MeasStart']
                for i in range(1, len(ends)):
                    startsi = ends[i-1] if ends[i-1] else startsi
                df['MeasStart'] = starts
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

        # (5) canonical columns first
        keep = ['Position', 'VialPos', 'Cycle', 'Repeat', 'CountTime', 'CPM', 'QIP']
        for c in keep:
            if c not in df.columns:
                df[c] = np.nan
                
        df.attrs['cpm_is_net'] = mapping_dict.get('CPM_is_net', False)
        df.attrs['dpm_is_net'] = mapping_dict.get('DPM_is_net', False)
        
        # Store efficiency source preference
        if 'Eff_pct' in df.columns:
            df.attrs['has_efficiency'] = True
                            
        return df[keep + [c for c in df.columns if c not in keep]]

# -------------------------------
# ALOKA CSV PARSER STRATEGY
# -------------------------------


