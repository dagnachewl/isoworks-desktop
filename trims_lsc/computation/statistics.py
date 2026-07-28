"""
Statistical Computation Functions

Extracted from trims_lsc_details_gui.py
Date: 2026-02-08
CORRECTED VERSION - complete functions
"""
from __future__ import annotations

import pandas as pd
import numpy as np
from typing import Tuple, Optional
from sqlalchemy import text
from db_core import db_manager
from shared_utils import get_standard_activity, get_global_value
from sample_type_registry import STYPE_LSC_DW, STYPE_LSC_STD
from datetime import datetime


def compute_means(df):
    valid = df[~df['IsOutlier']]
    grp = valid.groupby('Position').agg({
        'CPM': ['mean','std','count'],
        'CountTime': 'sum',
        'QIP': 'mean'
    }).reset_index()
    grp.columns = ['Position','MeanCPM','StdCPM','Cycles','TotalTime','MeanQIP']
                
    def calc_unc(row):
        if row['TotalTime'] > 0:
            return (row['MeanCPM'] / row['TotalTime']) ** 0.5
        elif row['Cycles'] > 1 and row['StdCPM'] > 0:
            return row['StdCPM'] / (row['Cycles'] ** 0.5)
        return 0.0

    grp['UncCPM'] = grp.apply(calc_unc, axis=1)
    return grp

def _legacy_compute_run_params(run_id, means_df):
    """
    Computes weighted Mean Background (Type 1) and Efficiency (Type 2).
    Standardized scaling for efficiency: standard activity is taken on a 1000 mL basis,
    then multiplied by each vial's specific mass (kg).
    """
    bkg_mean = 0.0
    bkg_unc = 0.0
    eff = 0.0
    eff_unc = 0.0
    with db_manager.get_connection() as conn:
        # Get run start time for decay correction
        run_row = conn.execute(text('SELECT RunStartTime FROM TRIMS.LSCRun WHERE RunID = :rid'), {'rid': run_id}).fetchone()
        run_date = run_row.RunStartTime if run_row else datetime.now()
        if isinstance(run_date, str):
            try: run_date = datetime.fromisoformat(run_date)
            except: run_date = datetime.now()

        # LoadList join: SampleType, SampleID, Prefix, SampleAmount
        sql = """
        SELECT ll.PositionInRun  AS positioninrun,
               ll.SampleType     AS sampletype,
               a.SampleID        AS sampleid,
               a.Prefix          AS prefix,
               ll.SampleAmount   AS sampleamount
        FROM TRIMS.LSCLoadList ll INNER JOIN Analysis a ON ll.AnalysisID = a.AnalysisID
        WHERE RunID = :rid
        """
        with db_manager.get_engine().connect() as _sa_conn:
            load_list = pd.read_sql(text(sql), _sa_conn, params={'rid': run_id})
        merged = means_df.merge(load_list, left_on='Position', right_on='positioninrun', how='left')

        # 1. Background (Dead Water — STYPE_LSC_DW)
        bkgs = merged[merged['sampletype'] == STYPE_LSC_DW]
        if not bkgs.empty:
            vals = bkgs['MeanCPM'].values
            uncs = bkgs['UncCPM'].values
            weights = 1.0 / (uncs**2 + 1e-12)
            bkg_mean = float(np.sum(vals * weights) / np.sum(weights))
            bkg_unc = float(np.sqrt(1.0 / np.sum(weights)))

        # 2. Efficiency (Standard — STYPE_LSC_STD)
        stds = merged[merged['sampletype'] == STYPE_LSC_STD]
        if not stds.empty:
            eff_vals, eff_uncs = [], []
            vol_unc_g = float(get_global_value("VOLUME_UNCERTAINTY_COCKTAIL") or 0.0)
            for _, row in stds.iterrows():
                net_cpm = max(0, row['MeanCPM'] - bkg_mean)
                if net_cpm <= 0:
                    continue
                sid = row['sampleid']
                if not sid:
                    continue
                # --- Standardized Scaling: 1000 mL basis * vial mass (kg) ---
                true_dpm_kg, true_dpm_kg_unc, _, _ = get_standard_activity(
                    conn, sid, run_date, "DPM", Amount=1000, AmountUnc=0
                )
                vial_mass_kg = float(row['sampleamount'] or 0.0) / 1000.0
                total_true_dpm = true_dpm_kg * vial_mass_kg
                total_true_dpm_unc = true_dpm_kg_unc * vial_mass_kg

                if total_true_dpm > 0:
                    e = net_cpm / total_true_dpm
                    rel_net  = (row['UncCPM'] / net_cpm)**2 if net_cpm else 0
                    rel_true = (total_true_dpm_unc / total_true_dpm)**2 if total_true_dpm else 0
                    u = e * np.sqrt(rel_net + rel_true)
                    eff_vals.append(e); eff_uncs.append(u)

            if eff_vals:
                eff_vals = np.array(eff_vals); eff_uncs = np.array(eff_uncs)
                weights = 1.0 / (eff_uncs**2 + 1e-12)
                eff = float(np.sum(eff_vals * weights) / np.sum(weights))
                eff_unc = float(np.sqrt(1.0 / np.sum(weights)))

    return bkg_mean, bkg_unc, eff, eff_unc

def compute_run_params(run_id, means_df):
    """
    Updated run-level background & efficiency computation:

      - Background computed in CPM only
      - net_cpm clipped at 0
      - Efficiency from standards using:
            net_cpm, true_dpm_kg, vial_mass_kg_uncertainty
    """

    bkg_mean = 0.0
    bkg_unc  = 0.0
    eff_run  = 0.0
    eff_unc  = 0.0

    with db_manager.get_connection() as conn:

        # Load sample types and SampleAmount
        sql = """
            SELECT ll.PositionInRun  AS positioninrun,
                   ll.SampleType     AS sampletype,
                   a.SampleID        AS sampleid,
                   ll.SampleAmount   AS sampleamount
            FROM TRIMS.LSCLoadList ll
            JOIN Analysis a ON ll.AnalysisID = a.AnalysisID
            WHERE ll.RunID = :rid
        """
        with db_manager.get_engine().connect() as _sa_conn:
            load = pd.read_sql(text(sql), _sa_conn, params={'rid': run_id})
        df = means_df.merge(load, left_on='Position', right_on='positioninrun', how='left')

        # --- Background (Dead Water — STYPE_LSC_DW) ---
        bkg = df[df['sampletype'] == STYPE_LSC_DW]
        if not bkg.empty:
            vals = bkg["MeanCPM"].astype(float)
            bkg_mean = vals.mean()
            bkg_unc  = vals.std(ddof=1) if len(vals) >= 2 else 0.0

        # --- Efficiency from standards ---
        eff_vals = []
        eff_uncs = []

        # Resolve run date
        run_dt = conn.execute(
            text("SELECT RunStartTime FROM TRIMS.LSCRun WHERE RunID=:rid"),
            {'rid': run_id}
        ).scalar()

        if isinstance(run_dt, str):
            try: run_dt = datetime.fromisoformat(run_dt)
            except: run_dt = datetime.now()

        for _, r in df[df['sampletype'] == STYPE_LSC_STD].iterrows():

            gross_cpm = float(r.MeanCPM or 0.0)
            net_cpm   = gross_cpm - bkg_mean
            if net_cpm < 0:
                net_cpm = 0.0

            unc_cpm = float(r.UncCPM or 0.0)
            net_unc = (unc_cpm**2 + bkg_unc**2)**0.5 if net_cpm > 0 else 0.0

            sid = r['sampleid']
            if not sid:
                continue

            true_dpm_kg, true_dpm_kg_unc, _, _ = get_standard_activity(
                conn, sid, run_dt, "DPM", Amount=1000, AmountUnc=0
            )

            mass_kg = float(r['sampleamount'] or 0.0) / 1000.0

            # vial mass uncertainty
            try:
                vol_unc_g = float(get_global_value("VOLUME_UNCERTAINTY_COCKTAIL") or 0.0)
            except:
                vol_unc_g = 0.0
            mass_unc_kg = vol_unc_g / 1000.0

            if true_dpm_kg > 0 and mass_kg > 0:
                total_true = true_dpm_kg * mass_kg
                e = net_cpm / total_true

                rel_true = (
                    (true_dpm_kg_unc / true_dpm_kg)**2 +
                    (mass_unc_kg / mass_kg)**2
                )
                rel_net = (net_unc / net_cpm)**2 if net_cpm > 0 else 0.0
                u = e * ((rel_true + rel_net)**0.5)

                eff_vals.append(e)
                eff_uncs.append(u)

        if eff_vals:
            eff_vals = np.array(eff_vals)
            eff_uncs = np.array(eff_uncs)
            w = 1 / np.where(eff_uncs > 0, eff_uncs, 1e10)**2
            eff_run = float(np.sum(eff_vals * w) / np.sum(w))
            eff_unc = float((1 / np.sum(w))**0.5)

        return bkg_mean, bkg_unc, eff_run, eff_unc
        
def _compute_net_activity_dpm_row(
    mean_cpm: float,
    unc_cpm: float,
    bkg_cpm: float,
    bkg_cpm_unc: float = 0.0,
    eff_frac: float = 1.0,
    eff_unc_frac: float = 0.0,
    sample_amount_g: float = 0.0,
    sample_diluent_g: float = 0.0,
    sample_amount_unc_g: float = 0.0,
    is_net: bool = False
) -> Tuple[float, float]:
    """
    Compute massic activity A_enr (DPM/kg) and uncertainty.
    Applies the user's final rule:
      - net = max(0, mean - background)
      - background always in CPM domain (converted for DPM mode)
      - DPM mode: eff_frac = 1, eff_unc_frac = 0
    """

    # --- Net CPM/DPM (depending on caller) ---
    # gross = float(mean_cpm or 0.0)
    # bkg   = float(bkg_cpm or 0.0)
    # net   = gross - bkg

    if is_net:
        # Already NET from instrument (e.g., HIDEX Matrix)
        net = mean_cpm
        net_unc = unc_cpm
        # Background already subtracted, so set to zero for uncertainty calc
        bkg_cpm = 0.0
    else:
        # GROSS - subtract background
        net = mean_cpm - bkg_cpm    
        # --- Uncertainty ---
        unc_gross = float(unc_cpm or 0.0)
        unc_bkg   = float(bkg_cpm_unc or 0.0)
        net_unc   = (unc_gross**2 + unc_bkg**2) ** 0.5

    if net < 0:
        net = 0.0
        
    # --- Mass (kg) ---
    raw_g   = (sample_amount_g or 0.0) - (sample_diluent_g or 0.0)
    mass_kg = max(raw_g, 0.001) / 1000.0

    # mass uncertainty
    mass_unc_g = float(sample_amount_unc_g or 0.0)
    if (sample_diluent_g or 0.0) > 0:
        mass_unc_g = (2 * (mass_unc_g**2)) ** 0.5
    mass_unc_kg = mass_unc_g / 1000.0

    # Efficiency guard
    if eff_frac <= 0:
        return 0.0, 0.0

    # --- DPM/kg ---
    A_enr = (net / eff_frac) / mass_kg

    # Relative uncertainties
    rel_net  = (net_unc / net) if net > 0 else 0.0
    rel_eff  = (eff_unc_frac / eff_frac) if eff_frac > 0 else 0.0
    rel_mass = (mass_unc_kg / mass_kg) if mass_kg > 0 else 0.0

    A_enr_unc = abs(A_enr) * ((rel_net**2 + rel_eff**2 + rel_mass**2) ** 0.5)

    return float(A_enr), float(A_enr_unc)

