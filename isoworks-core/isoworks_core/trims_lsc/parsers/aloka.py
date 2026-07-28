"""
Aloka LSC Parser

Extracted from trims_lsc_details_gui.py
Date: 2026-02-08 10:52:29
"""
from __future__ import annotations

import pandas as pd
import datetime as dt
import re
import csv
import numpy as np
from typing import Optional, Dict, List
from .base import LSCParserStrategy


class AlokaCsvParserStrategy(LSCParserStrategy):
    """
    Parser for Aloka CSV exports (based on the provided MS Access/VBA logic).
    Features:
      - Detects delimiter (',' or ';'), preserves empty cells, strips quotes/asterisks.
      - Finds the first header line containing "My No" and skips any repeated headers later.
      - Maps SN->Position, RN->Cycle, TIME/COUNT_TIME->CountTime.
      - Canonical CPM = A-CPM (configurable); Canonical DPM = A-DPM.
      - QIP prefers ESCR, falls back to SCCR.
      - Keeps A/B/C GROSS/CPM/DPM/EFF as extras.
      - Stores metadata in df.attrs: aloka_start_time/end_time/total_minutes/max_cycle.
    """

    DEFAULT_HEADER = (
        "My No;SN;RN;ESCR;SINGLE;COUNT_TIME;"
        "A-GROSS;A-CPM;A-DPM;A-EFF;"
        "B-GROSS;B-CPM;B-DPM;B-EFF;"
        "C-GROSS;C-CPM;C-DPM;C-EFF;"
        "SCCR;COUNT_DATE;COUNT_HOUR"
    )

    def __init__(self, filepath: str, header_to_read: Optionalstr = None, cpm_channel: str = "A", dpm_channel: str = "A"):
        super().__init__(filepath)
        self.header_to_read = (header_to_read or self.DEFAULT_HEADER).replace(",", ";")
        self.cpm_channel = (cpm_channel or "A").upper().strip()  # 'A'|'B'|'C'
        self.dpm_channel = (dpm_channel or "A").upper().strip()  # 'A'|'B'|'C'

    # ---------- helpers ----------
    @staticmethod
    def _detect_delimiter(sample: str) -> str:
        sc, cm = sample.count(";"), sample.count(",")
        return ";" if sc >= cm else ","

    @staticmethod
    def _split(line: str, delim: str) -> List[str]:
        # csv.reader preserves empties reliably
        reader = csv.reader(line, delimiter=delim)
        return next(reader)

    @staticmethod
    def _tok(s: str) -> str:
        return (s or "").strip()

    @staticmethod
    def _is_number(v: str) -> bool:
        try:
            float(str(v).strip())
            return True
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return False

    @staticmethod
    def _to_float(v: str) -> float:
        s = str(v).strip()
        if s == "":
            return float("nan")
        # tolerate decimal commas (only if not the delimiter)
        s_norm = s.replace(",", ".") if ("," in s and "." not in s) else s
        try:
            return float(s_norm)
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return float("nan")

    @staticmethod
    def _parse_date(s: str) -> Optional[dt.date]:
        s = (s or "").strip().replace("\\", "/")
        for fmt in ("%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(s, fmt).date()
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); continue
        return None

    @staticmethod
    def _parse_time(s: str) -> Optional[dt.time]:
        s = (s or "").strip()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return dt.datetime.strptime(s, fmt).time()
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); continue
        # numeric hour -> HH:00 fallback
        try:
            hh = int(float(s))
            return dt.time(hour=max(0, min(23, hh)))
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return None

    def _header_index_map(self, headers: List[str]) -> Dict[str, int]:
        norm = {self._tok(h).upper(): i for i, h in enumerate(headers)}
        # Add flexible aliases (files sometimes use TIME vs COUNT_TIME, etc.)
        aliases = {
            "COUNT_TIME": norm.get("COUNT_TIME", norm.get("TIME", -1)),
            "TIME": norm.get("TIME", norm.get("COUNT_TIME", -1)),
            "COUNT_DATE": norm.get("COUNT_DATE", -1),
            "COUNT_HOUR": norm.get("COUNT_HOUR", -1),
            "SN": norm.get("SN", -1),
            "RN": norm.get("RN", -1),
            "ESCR": norm.get("ESCR", -1),
            "SCCR": norm.get("SCCR", -1),
            "A-CPM": norm.get("A-CPM", -1),
            "A-DPM": norm.get("A-DPM", -1),
            "A-GROSS": norm.get("A-GROSS", -1),
            "A-EFF": norm.get("A-EFF", -1),
            "B-CPM": norm.get("B-CPM", -1),
            "B-DPM": norm.get("B-DPM", -1),
            "B-GROSS": norm.get("B-GROSS", -1),
            "B-EFF": norm.get("B-EFF", -1),
            "C-CPM": norm.get("C-CPM", -1),
            "C-DPM": norm.get("C-DPM", -1),
            "C-GROSS": norm.get("C-GROSS", -1),
            "C-EFF": norm.get("C-EFF", -1),
            "SMPL_ID": norm.get("SMPL_ID", -1),
        }
        return aliases

    def get_headers(self) -> list:
        with open(self.filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
        header_line = None
        for ln in lines[:200]:
            s = ln.replace('"', "")
            if "My No" in s:
                header_line = s
                break
        if not header_line:
            return [c.strip() for c in self.DEFAULT_HEADER.split(";")]
        delim = self._detect_delimiter(header_line)
        headers = self._split(header_line, delim)
        return [self._tok(h) for h in headers]

    def parse(self, mapping_dict: dict, count_time_unit: int = 1) -> pd.DataFrame:
        with open(self.filepath, "r", encoding="utf-8", errors="ignore") as f:
            raw_lines = f.read().splitlines()

        # Find first header line containing "My No"
        first_header_idx = None
        for i, ln in enumerate(raw_lines):
            if "My No" in ln:
                first_header_idx = i
                break

        header_line = (
            raw_lines[first_header_idx].replace('"', "") if first_header_idx is not None else self.DEFAULT_HEADER
        )
        delim = self._detect_delimiter(header_line)

        template_cols = [self._tok(x) for x in self.DEFAULT_HEADER.split(";")]
        headers_from_file = [self._tok(x) for x in self._split(header_line, delim)]
        has_header = ("SN" in [h.upper() for h in headers_from_file])
        use_template = (not has_header) or (len(headers_from_file) != len(template_cols))
        headers = template_cols if use_template else headers_from_file

        key = self._header_index_map(headers)

        max_cycle = -1
        total_minutes = 0.0
        start_time: Optional[dt.datetime] = None
        end_time: Optional[dt.datetime] = None

        records: ListDict = []
        start_idx = (first_header_idx + 1) if first_header_idx is not None else 0

        # For files like your sample where headers re-appear or have blanks after SCCR,
        # detect if trailing fields look like date/time and treat them accordingly.
        date_pat = re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$")

        for ln in raw_lines[start_idx:]:
            s = ln.replace('"', "").replace("*", "0").strip()
            if not s:
                continue
            # skip any repeated header-like line inside the body
            if "SN" in s and "RN" in s:
                continue

            cols = self._split(s, delim)

            # If template used and row is shorter, right-pad empties
            if use_template and len(cols) < len(headers):
                cols = cols + [""] * (len(headers) - len(cols))

            # Position & Cycle
            pos_val = cols[key["SN"]] if key["SN"] >= 0 and key["SN"] < len(cols) else ""
            cyc_val = cols[key["RN"]] if key["RN"] >= 0 and key["RN"] < len(cols) else ""
            if not self._is_number(pos_val) or not self._is_number(cyc_val):
                continue  # non-data line

            position = int(float(pos_val))
            cycle = int(float(cyc_val))
            repeat = 1

            # Count time (assumed minutes unless count_time_unit==2 says seconds)
            t_col = key["COUNT_TIME"] if key["COUNT_TIME"] >= 0 else key["TIME"]
            ct_raw = cols[t_col] if t_col >= 0 and t_col < len(cols) else ""
            count_time = self._to_float(ct_raw)
            if count_time == count_time:  # not NaN
                total_minutes += (count_time if int(count_time_unit) == 1 else count_time / 60.0)

            # Channel data
            a_cpm = self._to_float(cols[key["A-CPM"]]) if key["A-CPM"] >= 0 and key["A-CPM"] < len(cols) else float("nan")
            b_cpm = self._to_float(cols[key["B-CPM"]]) if key["B-CPM"] >= 0 and key["B-CPM"] < len(cols) else float("nan")
            c_cpm = self._to_float(cols[key["C-CPM"]]) if key["C-CPM"] >= 0 and key["C-CPM"] < len(cols) else float("nan")

            a_dpm = self._to_float(cols[key["A-DPM"]]) if key["A-DPM"] >= 0 and key["A-DPM"] < len(cols) else float("nan")
            b_dpm = self._to_float(cols[key["B-DPM"]]) if key["B-DPM"] >= 0 and key["B-DPM"] < len(cols) else float("nan")
            c_dpm = self._to_float(cols[key["C-DPM"]]) if key["C-DPM"] >= 0 and key["C-DPM"] < len(cols) else float("nan")

            # Canonical CPM/DPM selection: A- by default
            cpm_choice = {"A": a_cpm, "B": b_cpm, "C": c_cpm}.get(self.cpm_channel, a_cpm)
            if not (cpm_choice == cpm_choice):
                for v in (a_cpm, b_cpm, c_cpm):
                    if v == v:
                        cpm_choice = v
                        break

            dpm_choice = {"A": a_dpm, "B": b_dpm, "C": c_dpm}.get(self.dpm_channel, a_dpm)
            if not (dpm_choice == dpm_choice):
                for v in (a_dpm, b_dpm, c_dpm):
                    if v == v:
                        dpm_choice = v
                        break

            # QIP: ESCR preferred, else SCCR
            escr = self._to_float(cols[key["ESCR"]]) if key["ESCR"] >= 0 and key["ESCR"] < len(cols) else float("nan")
            sccr = self._to_float(cols[key["SCCR"]]) if key["SCCR"] >= 0 and key["SCCR"] < len(cols) else float("nan")
            qip_val = escr if escr == escr else sccr

            # COUNT_DATE / COUNT_HOUR may be absent in header; detect trailing date/time if needed
            d_raw = cols[key["COUNT_DATE"]] if key["COUNT_DATE"] >= 0 and key["COUNT_DATE"] < len(cols) else ""
            h_raw = cols[key["COUNT_HOUR"]] if key["COUNT_HOUR"] >= 0 and key["COUNT_HOUR"] < len(cols) else ""
            if (not d_raw or not h_raw) and len(cols) >= 2:
                # try last two fields as date, hour if they look like it
                if date_pat.match(cols[-2].strip()):
                    d_raw = cols[-2]
                    h_raw = cols[-1]

            d_date = self._parse_date(d_raw)
            d_time = self._parse_time(h_raw)
            dt_stamp = None
            if d_date:
                dt_stamp = dt.datetime.combine(d_date, d_time or dt.time(0, 0))

            if position == 1 and cycle == 1 and dt_stamp:
                start_time = dt_stamp
            if dt_stamp:
                end_time = dt_stamp
            if cycle > max_cycle:
                max_cycle = cycle

            rec = {
                'Position': position,
                'Cycle': cycle,
                'Repeat': repeat,
                'CountTime': (count_time if int(count_time_unit) == 1 else count_time / 60.0),
                'CPM': cpm_choice,     # canonical CPM = A-CPM by default
                'DPM': dpm_choice,     # canonical DPM = A-DPM by default
                'QIP': qip_val,
                # Extras preserved for transparency
                'A_GROSS': self._to_float(cols[key["A-GROSS"]]) if key["A-GROSS"] >= 0 and key["A-GROSS"] < len(cols) else float("nan"),
                'A_CPM': a_cpm, 'A_DPM': a_dpm, 'A_EFF': self._to_float(cols[key["A-EFF"]]) if key["A-EFF"] >= 0 and key["A-EFF"] < len(cols) else float("nan"),
                'B_GROSS': self._to_float(cols[key["B-GROSS"]]) if key["B-GROSS"] >= 0 and key["B-GROSS"] < len(cols) else float("nan"),
                'B_CPM': b_cpm, 'B_DPM': b_dpm, 'B_EFF': self._to_float(cols[key["B-EFF"]]) if key["B-EFF"] >= 0 and key["B-EFF"] < len(cols) else float("nan"),
                'C_GROSS': self._to_float(cols[key["C-GROSS"]]) if key["C-GROSS"] >= 0 and key["C-GROSS"] < len(cols) else float("nan"),
                'C_CPM': c_cpm, 'C_DPM': c_dpm, 'C_EFF': self._to_float(cols[key["C-EFF"]]) if key["C-EFF"] >= 0 and key["C-EFF"] < len(cols) else float("nan"),
                'ESCR': escr, 'SCCR': sccr,
                'COUNT_DATE': d_raw, 'COUNT_HOUR': h_raw
            }
            rec['Eff_pct']  = self._to_float(cols[key["A-EFF"]]) if key["A-EFF"] >= 0 and key["A-EFF"] < len(cols) else float("nan")
            rec['Eff_frac'] = (rec['Eff_pct'] / 100.0) if (rec.get('Eff_pct') == rec.get('Eff_pct')) else float("nan")
            records.append(rec)

        if not records:
            raise RuntimeError("Aloka CSV parser: no data rows found after header detection.")

        df = pd.DataFrame.from_records(records)

        # Normalize time unit if caller indicates Seconds (2)
        df = self._apply_time_unit(df, count_time_unit)

        # Ensure vial labels & run order
        df = self._ensure_vialpos(df, pos_col='Position')
        df = self._assign_positions(df, vial_col='VialPos', mode='sequence')  # file order
        df = self._standardize_cycles(df)

        # Canonical columns first
        canonical = ['Position', 'VialPos', 'Cycle', 'Repeat', 'CountTime', 'CPM', 'QIP', 'DPM']
        for c in canonical:
            if c not in df.columns:
                df[c] = np.nan
        df = df[canonical + [c for c in df.columns if c not in canonical]]

        # Metadata
        df.attrs['aloka_start_time'] = (start_time.isoformat(sep=' ') if start_time else '')
        df.attrs['aloka_end_time'] = (end_time.isoformat(sep=' ') if end_time else '')
        df.attrs['aloka_total_minutes'] = float(total_minutes)
        df.attrs['aloka_max_cycle'] = int(max_cycle)

        return df


