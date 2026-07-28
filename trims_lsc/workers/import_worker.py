"""
LSC Import Worker Thread

Extracted from trims_lsc_details_gui.py
Date: 2026-02-08 10:52:29
"""

import os
import logging
import pandas as pd
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal
from ..parsers.factory import make_lsc_parser
from ..parsers.quantulus import QuantulusRegistryParserModern, iter_cycles
from ..parsers.other import HidexLSCParser, PackardLSCParser


class LSCImportWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str, object)  # success, msg, df_with_flags

    def __init__(self, run_id, file_path, format_id, settings):
        super().__init__()
        self.run_id = run_id
        self.file_path = file_path
        self.format_id = format_id
        self.settings = settings

    def run(self):
        try:
            self.progress.emit(f"Reading file: {os.path.basename(self.file_path)}…")

            # 1) Make parser from Factory (fmt_name is passed via settings for convenience)
            fmt_name = self.settings.get('format_name', "")
            parser = make_lsc_parser(self.format_id, fmt_name, self.file_path)

            # 2) Unified mapping + time unit
            mapping = self.settings.get('mapping', {})  # UI builds this per-format
            count_time_unit = int(self.settings.get('count_time_unit', 1))  # 1=minutes, 2=seconds

            if isinstance(parser, QuantulusRegistryParserModern):
                raw_df = parser.parse(mapping_dict=mapping)
            else:
                raw_df = parser.parse(mapping_dict=mapping, count_time_unit=count_time_unit)
                
            if raw_df is None or raw_df.empty:
                raise ValueError('No cycles were parsed from the file.')

            # 4) Outliers (preview-only; your existing logic is kept)
            if bool(self.settings.get('apply_outliers', False)):
                self.progress.emit("Detecting outliers…")
                out_df = self._detect_outliers(raw_df, self.settings)
            else:
                out_df = raw_df
            
            # Apply protocol NET flags to DataFrame attributes
            if 'protocol' in self.settings and self.settings.get('protocol'):
                protocol = self.settings['protocol']
                for mapping in protocol.mappings:
                    if mapping.target_field == 'CPM' and mapping.is_net:
                        out_df.attrs['cpm_is_net'] = True
                        logging.info("CPM marked as NET (from protocol)")
                    if mapping.target_field == 'DPM' and mapping.is_net:
                        out_df.attrs['dpm_is_net'] = True
                        logging.info("DPM marked as NET (from protocol)")
            else:
                # Fallback to settings dict NET flags
                out_df.attrs['cpm_is_net'] = self.settings.get('net_cpm', False)
                out_df.attrs['dpm_is_net'] = self.settings.get('net_dpm', False)
            
            self.finished.emit(True, 'Import completed. Review data and click Step 2 (Compute).', out_df)

        except Exception as e:
            logging.error(f"LSC Import Error: {e}", exc_info=True)
            self.finished.emit(False, str(e), None)

    def _parse_hidex_csv(self, path, settings):
        """Wrapper that uses HidexLSCParser and applies UI settings."""
        parser = HidexLSCParser(path, separator='auto')
        df = parser.parse() # This now handles header detection and standardization

        if df.empty:
            raise ValueError("Hidex parser returned an empty dataset.")

        # 1. Apply UI-selected ROI mapping
        # Instead of hardcoding 'CPMroi1', use what the user picked in the combo box
        win_cpm_col = settings.get('win_cpm')
        if win_cpm_col and win_cpm_col in df.columns:
            df['CPM'] = pd.to_numeric(df[win_cpm_col], errors='coerce')
        
        win_qip_col = settings.get('win_qip')
        if win_qip_col and win_qip_col in df.columns:
            df['QIP'] = pd.to_numeric(df[win_qip_col], errors='coerce')

        # 2. Ensure essential TRIMS columns exist
        required_cols = ['Position', 'Cycle', 'Repeat', 'CountTime', 'CPM']
        for col in required_cols:
            if col not in df.columns:
                if col == 'Repeat': df['Repeat'] = 1
                elif col == 'CountTime': df['CountTime'] = 1.0
                else: raise ValueError(f"Required column '{col}' missing from Hidex data.")

        # 3. Handle Time Units from UI
        if int(settings.get('count_time_unit', 1)) == 2: # Seconds to Minutes
            df['CountTime'] = df['CountTime'] / 60.0

        return df.dropna(subset=['Position', 'CPM'])

    def _parse_quantulus_registry(self, path, settings):
        with open(path, 'r', encoding='latin1', errors='ignore') as f:
            lines = f.read().splitlines()

        records = list(iter_cycles(lines, instrument_no=None))
        if not records:
            raise ValueError("No Quantulus cycles were found in the file (header/format mismatch).")

        df = pd.DataFrame(records)
        if 'cpms' not in df.columns:
            raise ValueError("Quantulus registry file has no CPM window data ('cpms'). Check header/WINDOW blocks.")

        # Window selection (defaults preserved by the dialog; we only read here)
        win_cpm = settings.get('win_cpm')
        if win_cpm is None:
            win_cpm = 1
        df['CPM'] = df['cpms'].apply(lambda xs: xs[win_cpm-1] if xs and len(xs) >= win_cpm else np.nan)

        win_qip = settings.get('win_qip')
        df['QIP'] = pd.to_numeric(df['SQP'], errors='coerce') if win_qip is not None else np.nan

        df = df[['Position','Cycle','Repeat','CountTime','CPM','QIP','SQP','SQP_pct','meas_file','start_datetime','stime']].copy()

        # Keep step-1 in minutes unless user picked "Seconds"
        if int(settings.get('count_time_unit', 1)) == 2:
            df['CountTime'] = df['CountTime'] * 60.0

        return df

    def _parse_packard_csv(self, path, settings):
        mapping = settings.get('mapping') 
        parser = PackardLSCParser(path)
        df = parser.parse(mapping) # Standardized columns exist now
        if int(settings.get('count_time_unit', 1)) == 2:
            df['CountTime'] = df['CountTime'] / 60.0
        return df
    
    def _parse_generic_csv(self, path, settings):
        sep = settings.get('separator', ',')
        try:
            if path.endswith('.xls') or path.endswith('.xlsx'):
                df = pd.read_excel(path)
            else:
                df = pd.read_csv(path, sep=sep, engine='python')
            df.columns = [str(c).strip() for c in df.columns]
            col_map = settings.get('col_map', {})
            out_df = pd.DataFrame()

            def get_col(key):
                c = col_map.get(key)
                return pd.to_numeric(df[c], errors='coerce') if c and c in df.columns else None

            out_df['Position']   = get_col('pos') if get_col('pos') is not None else df.index + 1
            out_df['CPM']        = get_col('cpm')
            out_df['CountTime']  = get_col('time') if get_col('time') is not None else 1.0
            out_df['Cycle']      = get_col('cycle')
            if out_df['Cycle'].isnull().all():
                out_df['Cycle'] = out_df.groupby('Position').cumcount() + 1
            out_df['QIP'] = get_col('qip') if get_col('qip') is not None else np.nan
            out_df['SQP'] = np.nan; out_df['SQP_pct'] = np.nan
            out_df['meas_file'] = ''
            out_df['start_datetime'] = ''
            out_df['stime'] = ''
            
            if out_df['CPM'].isnull().all():
                raise RuntimeError('No CPM data found. Select a CPM window or provide CPM column in Generic file.')
            return out_df.dropna(subset=['Position', 'CPM'])
        
        except Exception as e:
            raise RuntimeError(f"File parsing error: {e}")
    
    def _detect_outliers(self, df, settings):
        """
        Enhanced outlier detection with multiple methods:
        - None: No outlier detection
        - Chauvenet: Probabilistic criterion based on normal distribution
        - Modified Z-Score: Using MAD (robust to outliers)
        - X_STDV: Flexible sigma threshold (e.g., 2.0, 2.5, 3.0)
        - Grubbs: Grubbs' test for single outlier
        - Dixon: Dixon's Q-test
        - GESD: Generalized ESD test (multiple outliers)
        """
        method = settings.get('outlier_method', 'None')
        threshold = settings.get('outlier_param', 2.0)
        target_col = settings.get('outlier_on', 'CPM')

        df = df.copy()
        df['IsOutlier'] = False
        
        if method == 'None' or target_col not in df.columns:
            return df

        for pos, group in df.groupby('Position'):
            if len(group) < 3:
                continue
            
            vals = pd.to_numeric(group[target_col], errors='coerce').dropna().values
            if len(vals) < 3:
                continue

            outlier_mask = np.zeros(len(group), dtype=bool)
            valid_indices = group[target_col].notna()
            
            # --- Chauvenet's Criterion ---
            if method == 'Chauvenet':
                from scipy.stats import norm
                mean = np.mean(vals)
                std = np.std(vals, ddof=1)
                if std > 0:
                    n = len(vals)
                    # Probability threshold: 1/(2N)
                    prob_threshold = 1.0 / (2.0 * n)
                    # Z-score for this probability
                    z_threshold = abs(norm.ppf(prob_threshold))
                    
                    z_scores = np.abs((group.loc[valid_indices, target_col].values - mean) / std)
                    outlier_mask[valid_indices] = z_scores > z_threshold
            
            # --- Modified Z-Score (MAD-based) ---
            elif method == 'Modified Z-Score':
                med = np.median(vals)
                mad = np.median(np.abs(vals - med))
                if mad > 0:
                    # Modified Z-score = 0.6745 * (x - median) / MAD
                    z = 0.6745 * np.abs(group.loc[valid_indices, target_col].values - med) / mad
                    outlier_mask[valid_indices] = z > threshold
            
            # --- X_STDV (Flexible sigma threshold: 2.0, 2.5, 3.0, etc.) ---
            elif method == 'X_STDV':
                mean = np.mean(vals)
                std = np.std(vals, ddof=1)
                if std > 0:
                    z = np.abs(group.loc[valid_indices, target_col].values - mean) / std
                    outlier_mask[valid_indices] = z > threshold
            
            # --- Grubbs' Test (single outlier) ---
            elif method == 'Grubbs':
                from scipy.stats import t
                n = len(vals)
                mean = np.mean(vals)
                std = np.std(vals, ddof=1)
                if std > 0:
                    # Grubbs statistic G = max|x_i - mean| / std
                    G = np.max(np.abs(vals - mean)) / std
                    # Critical value at alpha=0.05 (two-tailed)
                    t_dist = t.ppf(1 - 0.05/(2*n), n-2)
                    G_critical = ((n-1) / np.sqrt(n)) * np.sqrt(t_dist**2 / (n-2+t_dist**2))
                    
                    if G > G_critical:
                        # Mark the most extreme value
                        extreme_idx = np.argmax(np.abs(vals - mean))
                        temp_mask = np.zeros(len(vals), dtype=bool)
                        temp_mask[extreme_idx] = True
                        outlier_mask[valid_indices] = temp_mask
            
            # --- Dixon's Q-test ---
            elif method == 'Dixon':
                sorted_vals = np.sort(vals)
                n = len(sorted_vals)
                if n >= 3:
                    # Dixon's Q statistic for smallest and largest
                    Q_low = (sorted_vals[1] - sorted_vals[0]) / (sorted_vals[-1] - sorted_vals[0])
                    Q_high = (sorted_vals[-1] - sorted_vals[-2]) / (sorted_vals[-1] - sorted_vals[0])

                    # Critical values (approximate, for n=3-10, alpha=0.05)
                    Q_critical = {3: 0.941, 4: 0.765, 5: 0.642, 6: 0.560, 
                                  7: 0.507, 8: 0.468, 9: 0.437, 10: 0.412}.get(n, 0.41)
                    
                    # Mark outliers
                    if Q_low > Q_critical:
                        outlier_mask[group[target_col] == sorted_vals[0]] = True
                    if Q_high > Q_critical:
                        outlier_mask[group[target_col] == sorted_vals[-1]] = True
            
            # --- GESD (Generalized ESD) ---
            elif method == 'GESD':
                from scipy.stats import t as t_dist
                max_outliers = int(round(threshold)) if threshold >= 1 else int(len(vals) * 0.1)
                
                remaining_vals = vals.copy()
                remaining_idx = np.arange(len(vals))
                detected_outliers = []
                
                for i in range(max_outliers):
                    if len(remaining_vals) < 3:
                        break
                    
                    n = len(remaining_vals)
                    mean = np.mean(remaining_vals)
                    std = np.std(remaining_vals, ddof=1)
                    
                    if std == 0:
                        break
                    
                    # Find max deviation
                    R = np.abs(remaining_vals - mean) / std
                    max_R_idx = np.argmax(R)
                    max_R = R[max_R_idx]
                    
                    # Critical value
                    alpha = 0.05 / (2 * (n - i))
                    t_val = t_dist.ppf(1 - alpha, n - 2)
                    lambda_i = ((n - 1) * t_val) / np.sqrt((n - 2 + t_val**2) * n)
                    
                    if max_R > lambda_i:
                        detected_outliers.append(remaining_idx[max_R_idx])
                        remaining_vals = np.delete(remaining_vals, max_R_idx)
                        remaining_idx = np.delete(remaining_idx, max_R_idx)
                    else:
                        break
                
                # Mark detected outliers
                if detected_outliers:
                    temp_mask = np.zeros(len(vals), dtype=bool)
                    for idx in detected_outliers:
                        temp_mask[idx] = True
                    outlier_mask[valid_indices] = temp_mask
            
            # Apply outlier mask
            df.loc[group.index[outlier_mask], 'IsOutlier'] = True
        
        return df

# =============================
# COMPUTATION HELPERS
# =============================
