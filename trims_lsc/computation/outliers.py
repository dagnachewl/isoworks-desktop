"""
Outlier Detection Methods

Extracted from trims_lsc_details_gui.py during automated refactoring
Date: 2026-02-08 10:40:11
"""


import pandas as pd
import numpy as np
from scipy.stats import norm, t


def detect_outliers(df: pd.DataFrame, method: str, threshold: float, 
                   target_col: str = 'CPM') -> pd.DataFrame:
    """
    Detect outliers using specified method.
    
    Args:
        df: DataFrame with measurement data
        method: Detection method (Chauvenet, Modified Z-Score, X_STDV, Grubbs, Dixon, GESD)
        threshold: Method-specific threshold parameter
        target_col: Column to analyze for outliers
    
    Returns:
        DataFrame with 'IsOutlier' boolean column added
    """
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
        
        # Chauvenet's Criterion
        if method == 'Chauvenet':
            mean = np.mean(vals)
            std = np.std(vals, ddof=1)
            if std > 0:
                n = len(vals)
                prob_threshold = 1.0 / (2.0 * n)
                z_threshold = abs(norm.ppf(prob_threshold))
                z_scores = np.abs((group.loc[valid_indices, target_col].values - mean) / std)
                outlier_mask[valid_indices] = z_scores > z_threshold
        
        # Modified Z-Score
        elif method == 'Modified Z-Score':
            med = np.median(vals)
            mad = np.median(np.abs(vals - med))
            if mad > 0:
                z = 0.6745 * np.abs(group.loc[valid_indices, target_col].values - med) / mad
                outlier_mask[valid_indices] = z > threshold
        
        # X_STDV
        elif method == 'X_STDV':
            mean = np.mean(vals)
            std = np.std(vals, ddof=1)
            if std > 0:
                z = np.abs(group.loc[valid_indices, target_col].values - mean) / std
                outlier_mask[valid_indices] = z > threshold
        
        # Grubbs' Test
        elif method == 'Grubbs':
            n = len(vals)
            mean = np.mean(vals)
            std = np.std(vals, ddof=1)
            if std > 0:
                G = np.max(np.abs(vals - mean)) / std
                t_dist = t.ppf(1 - 0.05/(2*n), n-2)
                G_critical = ((n-1) / np.sqrt(n)) * np.sqrt(t_dist**2 / (n-2+t_dist**2))
                if G > G_critical:
                    extreme_idx = np.argmax(np.abs(vals - mean))
                    temp_mask = np.zeros(len(vals), dtype=bool)
                    temp_mask[extreme_idx] = True
                    outlier_mask[valid_indices] = temp_mask
        
        # Dixon's Q-test
        elif method == 'Dixon':
            sorted_vals = np.sort(vals)
            n = len(sorted_vals)
            if n >= 3:
                Q_low = (sorted_vals[1] - sorted_vals[0]) / (sorted_vals[-1] - sorted_vals[0])
                Q_high = (sorted_vals[-1] - sorted_vals[-2]) / (sorted_vals[-1] - sorted_vals[0])
                Q_critical = {3: 0.941, 4: 0.765, 5: 0.642, 6: 0.560, 
                             7: 0.507, 8: 0.468, 9: 0.437, 10: 0.412}.get(n, 0.41)
                if Q_low > Q_critical:
                    outlier_mask[group[target_col] == sorted_vals[0]] = True
                if Q_high > Q_critical:
                    outlier_mask[group[target_col] == sorted_vals[-1]] = True
        
        # GESD
        elif method == 'GESD':
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
                
                R = np.abs(remaining_vals - mean) / std
                max_R_idx = np.argmax(R)
                max_R = R[max_R_idx]
                
                alpha = 0.05 / (2 * (n - i))
                t_val = t.ppf(1 - alpha, n - 2)
                lambda_i = ((n - 1) * t_val) / np.sqrt((n - 2 + t_val**2) * n)
                
                if max_R > lambda_i:
                    detected_outliers.append(remaining_idx[max_R_idx])
                    remaining_vals = np.delete(remaining_vals, max_R_idx)
                    remaining_idx = np.delete(remaining_idx, max_R_idx)
                else:
                    break
            
            if detected_outliers:
                temp_mask = np.zeros(len(vals), dtype=bool)
                for idx in detected_outliers:
                    temp_mask[idx] = True
                outlier_mask[valid_indices] = temp_mask
        
        df.loc[group.index[outlier_mask], 'IsOutlier'] = True
    
    return df
