"""
Helper functions and dialogs for LSC GUI

These were standalone functions in the original trims_lsc_details_gui.py
that are used by main_dialog.py
"""
from __future__ import annotations

import re
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional, List
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QComboBox, QPushButton, 
    QHBoxLayout, QCheckBox, QWidget, QGridLayout
)
from PyQt5.QtCore import Qt
from sqlalchemy import text
from db_core import db_manager
from shared_utils import (
    get_global_value,
    calculate_decay_factor,
    convert_activity_unit
)

from protocol_manager import ProtocolManager
# Import computation function
from ..computation.statistics import _compute_net_activity_dpm_row
from smart_formatting import format_value_uncertainty

# Regex for Quantulus CPM window parsing
_Q_CPM_RE = re.compile(r'^(?:CPM[_\-]?)?(\\d+)$', re.I)


def _update_lsc_run_mean(conn, count_id: int, kind: int, val: float | None, unc: float | None):
    """
    Upsert into TRIMS.LSCRunMean for a (CountID, ValueKind)
    """
    sql_sel = text("SELECT 1 FROM TRIMS.LSCRunMean WHERE CountID=:cid AND ValueKind=:k")
    exists = conn.execute(sql_sel, {'cid': count_id, 'k': kind}).fetchone()
    if exists:
        conn.execute(text(
            "UPDATE TRIMS.LSCRunMean SET MeanValue=:v, MeanValueUnc=:u "
            "WHERE CountID=:cid AND ValueKind=:k"),
            {'v': (val if val is not None else None),
             'u': (unc if unc is not None else None),
             'cid': count_id, 'k': kind}
        )
    else:
        conn.execute(text(
            "INSERT INTO TRIMS.LSCRunMean (CountID, ValueKind, MeanValue, MeanValueUnc) "
            "VALUES (:cid, :k, :v, :u)"),
            {'cid': count_id, 'k': kind,
             'v': (val if val is not None else None),
             'u': (unc if unc is not None else None)}
        )


def _canon_quantulus_source(val, kind='CPM'):
    """
    Normalize any legacy mapping value to canonical Quantulus tokens:
    - kind='CPM' -> 'CPM1', 'CPM2', ...
    - kind='QIP' -> always 'SQP'
    Accepts: 2, '2', 'F2', 'CPM_F2', 'CPM2', '9 — F9: SQP', etc.
    """
    if val is None:
        return None
    s = str(val).strip().upper()

    if kind == 'QIP':
        # Quantulus QIP is SQP
        return 'SQP'

    # Already canonical
    m = re.match(r'^CPM(\d+)$', s)
    if m:
        return f'CPM{int(m.group(1))}'

    # Extract any numeric part
    m = _Q_CPM_RE.search(s)
    if m:
        n = int(m.group(1))
        # Window 9 is SQP in many headers but for CPM it's still CPM9 when present
        return f'CPM{n}'

    # Display string like "CPM2: [100–180]" -> extract CPM2 on the left
    if ':' in s and s.startswith('CPM'):
        left = s.split(':', 1)[0]
        if re.match(r'^CPM\d+$', left):
            return left

    return None



def save_hidex_me_to_db(run_id: int, df_me: pd.DataFrame, data_path: str | None = None):
    """
    Persist HIDEX Matrix fitted means to TRIMS:
      - LSCRun: Start/End, CounterEfficiency, MeanBackground (Bg fit), status
      - LSCRunMean: kind 0 (gross CPM), 1 (net CPM fit), 11 (DPM), 90 (QPE)
      - LSCLoadList: Result/Unc (net CPM fit), CountTime
    Assumptions:
      * Efficiency is per-position in 'Eff_pct' (we'll take the nanmean)
      * Background fit is in 'MeanCPM_BgFit';
    """

    with db_manager.get_connection() as conn:
        # ---- run-level fields ----
        eff_frac = float(np.nanmean(pd.to_numeric(df_me.get('Eff_pct'), errors='coerce') / 100.0))
        bkg_fit_vec = pd.to_numeric(df_me.get('MeanCPM_BgFit'), errors='coerce')
        bkg_fit = float(bkg_fit_vec.iloc[0]) if not bkg_fit_vec.empty and not pd.isna(bkg_fit_vec.iloc[0]) else 0.0

        start_time = str(df_me.get('MeasStart').iloc[0]) if 'MeasStart' in df_me.columns and len(df_me) else None
        end_time   = str(df_me.get('MeasEnd').iloc[-1])  if 'MeasEnd'   in df_me.columns and len(df_me) else None

        conn.execute(text(
            "UPDATE TRIMS.LSCRun SET "
            "RunStartTime=:st, RunEndTime=:et, "
            "CounterEfficiency=:eff, CounterEfficiencyUnc=:effu, "
            "MeanBackground=:bg, MeanBackgroundUnc=:bgu, "
            "RunStatus=7, DataPath=COALESCE(:dp, DataPath) "
            "WHERE RunID=:rid"
        ), {
            'st': start_time, 'et': end_time,
            'eff': eff_frac, 'effu': eff_frac/100.0,  # keep placeholder
            'bg': bkg_fit, 'bgu': bkg_fit/100.0,      # idem
            'dp': data_path, 'rid': run_id
        })

        # ---- per-position means ----
        # Map PositionInRun -> CountID to write LSCRunMean
        rows = conn.execute(text(
            "SELECT PositionInRun, CountID FROM TRIMS.LSCLoadList WHERE RunID=:rid"
        ), {'rid': run_id}).fetchall()
        pos_to_count = {int(r.PositionInRun): int(r.CountID) for r in rows if r.CountID is not None}

        for _, r in df_me.iterrows():
            pos = int(r['Position'])
            cnt = pos_to_count.get(pos)
            if not cnt:
                continue

            gross_cpm = float(r['MeanCPM_Gross']) if not pd.isna(r['MeanCPM_Gross']) else None
            net_cpm   = float(r['NetCPM_fit']) if not pd.isna(r['NetCPM_fit']) else (
                        float(r['CPM']) if not pd.isna(r['CPM']) else None)
            net_unc   = float(r['NetCPM_unc']) if 'NetCPM_unc' in r and not pd.isna(r['NetCPM_unc']) else None
            dpm       = float(r['DPM']) if not pd.isna(r['DPM']) else None
            dpm_unc   = float(r['DPM_unc']) if not pd.isna(r['DPM_unc']) else None
            qpe_val   = float(r['QIP']) if not pd.isna(r['QIP']) else None
            counttime = float(r['CountTime']) if not pd.isna(r['CountTime']) else None

            # LSCRunMean kinds: 0=gross CPM, 1=net CPM, 11=DPM, 90=QPE
            _update_lsc_run_mean(conn, cnt, 0, gross_cpm, (gross_cpm / counttime)**0.5 if gross_cpm and counttime else None)
            _update_lsc_run_mean(conn, cnt, 1, net_cpm,   net_unc)
            _update_lsc_run_mean(conn, cnt, 11, dpm,      dpm_unc)
            _update_lsc_run_mean(conn, cnt, 90, qpe_val,  (qpe_val/100.0) if qpe_val is not None else None)

            # mirror to LSCLoadList
            conn.execute(text(
                "UPDATE TRIMS.LSCLoadList SET Result=:v, ResultUnc=:u, CountTime=:t "
                "WHERE RunID=:rid AND PositionInRun=:pos"
            ), {'v': net_cpm, 'u': net_unc, 't': counttime, 'rid': run_id, 'pos': pos})

        conn.commit()
        


class HidexMatrixMappingDialog(QDialog):
    """
    Advanced mapping dialog for HIDEX Matrix (FormatID=6):
    - For each signal (CPM, DPM, QIP, Full) let user select:
      * Source header
      * Net? (checkbox)
      * Uncertainty source (header)
    Saves as rows in GUItblImportMapping with keys:
      - 'CPM'|'DPM'|'QIP'|'Full' -> header
      - 'CPM_is_net'...'Full_is_net' -> '1'|'0'
      - 'CPM_unc'...'Full_unc' -> header (or '')
    """
    def __init__(self, source_headers: list[str], existing: dict | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Map HIDEX Matrix Signals")
        self.setMinimumWidth(520)
        existing = existing or {}
        self._rows = {}  # key -> {'src': QComboBox, 'net': QCheckBox, 'unc': QComboBox}

        def mk_row(grid: QGridLayout, row: int, label: str, key: str):
            grid.addWidget(QLabel(label + ":"), row, 0)

            cb_src = QComboBox(self)
            cb_src.addItem("-- Select Source --", "")
            for h in source_headers:
                cb_src.addItem(h, h)
            # preload
            if existing.get(key):
                i = cb_src.findText(existing[key])
                if i >= 0:
                    cb_src.setCurrentIndex(i)
            grid.addWidget(cb_src, row, 1)

            chk_net = QCheckBox("Net?", self)
            # preload
            chk_net.setChecked(str(existing.get(f"{key}_is_net", "0")).strip() in ("1", "true", "True", "yes", "y"))
            grid.addWidget(chk_net, row, 2)

            cb_unc = QComboBox(self)
            cb_unc.addItem("-- Uncertainty (optional) --", "")
            for h in source_headers:
                cb_unc.addItem(h, h)
            if existing.get(f"{key}_unc"):
                j = cb_unc.findText(existing[f"{key}_unc"])
                if j >= 0:
                    cb_unc.setCurrentIndex(j)
            grid.addWidget(cb_unc, row, 3)

            self._rows[key] = {'src': cb_src, 'net': chk_net, 'unc': cb_unc}

        lay = QVBoxLayout(self)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        mk_row(grid, 0, "CPM", "CPM")
        mk_row(grid, 1, "DPM", "DPM")
        mk_row(grid, 2, "QIP", "QIP")
        mk_row(grid, 3, "Full", "Full")
        lay.addLayout(grid)

        # Helper text for typical ME headers (purely informational)
        hint = QLabel("Hints: try 'CPM H-3 (cpm fit)' for CPM (Net), 'QPE' for QIP; "
                      "uncertainty often 'Unc.CPM H-3' (per position).")
        hint.setStyleSheet("color:#666;")
        lay.addWidget(hint)

        btns = QHBoxLayout()
        btns.addStretch(1)
        btn_ok = QPushButton("Save & Continue", self)
        btn_ok.clicked.connect(self.accept)
        btns.addWidget(btn_ok)
        lay.addLayout(btns)

    def get_mapping(self) -> dict:
        out = {}
        for key, widgets in self._rows.items():
            src = widgets['src'].currentData() or ""
            if src:
                out[key] = src
            out[f"{key}_is_net"] = "1" if widgets['net'].isChecked() else "0"
            unc = widgets['unc'].currentData() or ""
            if unc:
                out[f"{key}_unc"] = unc
        return out
                        


def calculate_and_save_final_activities(
    conn,
    run_id: int,
    bkg_cpm: float,           # kept for signature compatibility; NOT used because we read Net CPM
    eff_frac: float,
    activity_unit: int = 1,   # 1=TU, 2=DPM/kg, 3=Bq/kg, 4=pCi/L
    used_method: int = 1      # preferred EF method id; will be overridden per-row if deuterium/spike available
):
    """
    Final Activity pipeline (Net‑CPM driven):
      1) Read per‑vial Net CPM (and its unc) from TRIMS.LSCRunMean (ValueKind=1) via CountID
      2) Convert Net CPM -> A_enr (DPM/kg) using efficiency & vial mass, with uncertainty propagation
      3) Decay-correct to collection date
      4) Divide by EF -> pre-enrichment activity
      5) Convert to target unit & upsert in TRIMS.LSCResult

    Notes:
      * Net CPM comes from LSCRunMean.MeanValue (ValueKind=1) — DO NOT subtract background again.
      * This function does not write to LSCLoadList.Result; stable DPM/kg is handled during Step‑3 save.
    """
    if eff_frac is None or eff_frac <= 0:
        return

    # with db_manager.get_connection() as conn:
    # Run-level meta for uncertainties and run date
    run_row = conn.execute(text(
        "SELECT CounterEfficiencyUnc, RunStartTime FROM TRIMS.LSCRun WHERE RunID=:rid"
    ), {'rid': run_id}).fetchone()

    eff_unc_frac = float(getattr(run_row, 'CounterEfficiencyUnc', 0.0) or 0.0)
    run_date = run_row.RunStartTime if run_row else datetime.now()
    if isinstance(run_date, str):
        try:
            run_date = datetime.fromisoformat(run_date)
        except Exception:
            run_date = datetime.now()

    # Volume uncertainty (g) for mass propagation
    vol_unc_g = get_global_value("VOLUME_UNCERTAINTY_COCKTAIL") or 0.0
    try:
        vol_unc_g = float(vol_unc_g)
    except Exception:
        vol_unc_g = 0.0

    # --- Determine EF source from used_method (reliable, set by operator in UI) ---
    # Methods 1=Deuterium, 2=ElecSpikeEF, 4=Direct → tritium/electrolysis path (MeasurableID=3)
    # Methods 5=Gravimetric, 6=SpikeRecovery, 7=Volumetric → chemical enrichment path
    _CHEM_METHODS = {5, 6, 7}
    _measurable_id = 99 if int(used_method or 1) in _CHEM_METHODS else 3

    # --- Pull Net CPM from LSCRunMean (ValueKind=1) by CountID ---
    # Join through LSCLoadList so we also get AnalysisID, amounts, EF, etc.
    # For tritium (MeasurableID=3): also pull electrolysis + deuterium EF columns.
    # For other isotopes: pull chemical enrichment EF instead.
    if _measurable_id == 3:
        _ef_sql = """
        SELECT
            ll.PositionInRun,
            ll.AnalysisID,
            rm.MeanValue     AS MeanCPM,
            rm.MeanValueUnc  AS UncCPM,
            ll.SampleAmount,
            ll.SampleDiluent,
            ll.SampleType,
            s.CollectionDate,
            e.EnrichmentFactor,
            e.EnrichmentFactorUnc,
            d.EnrichmentFactor      AS H2EF,
            d.EnrichmentFactorUnc   AS H2EFunc,
            NULL                    AS ChemEF,
            NULL                    AS ChemEFUnc,
            NULL                    AS ChemMeth
        FROM TRIMS.LSCLoadList           ll
        LEFT JOIN TRIMS.LSCRunMean       rm ON rm.CountID = ll.CountID AND rm.ValueKind = 1
        INNER JOIN Analysis              a  ON ll.AnalysisID = a.AnalysisID
        INNER JOIN Sample                s  ON s.SampleID = a.SampleID AND s.Prefix = a.Prefix
        LEFT  JOIN TRIMS.Electrolysis    e  ON ll.AnalysisID = e.AnalysisID
        LEFT  JOIN TRIMS.DeuteriumEnrichment d ON d.ElectrolysisID = e.ElectrolysisID
        WHERE ll.RunID = :rid
        ORDER BY ll.PositionInRun
        """
    else:
        _ef_sql = """
        SELECT
            ll.PositionInRun,
            ll.AnalysisID,
            rm.MeanValue     AS MeanCPM,
            rm.MeanValueUnc  AS UncCPM,
            ll.SampleAmount,
            ll.SampleDiluent,
            ll.SampleType,
            s.CollectionDate,
            NULL             AS EnrichmentFactor,
            NULL             AS EnrichmentFactorUnc,
            NULL             AS H2EF,
            NULL             AS H2EFunc,
            ce.EnrichmentFactor     AS ChemEF,
            ce.EnrichmentFactorUnc  AS ChemEFUnc,
            ce.EnrichmentMethod     AS ChemMeth
        FROM TRIMS.LSCLoadList           ll
        LEFT JOIN TRIMS.LSCRunMean       rm ON rm.CountID = ll.CountID AND rm.ValueKind = 1
        INNER JOIN Analysis              a  ON ll.AnalysisID = a.AnalysisID
        INNER JOIN Sample                s  ON s.SampleID = a.SampleID AND s.Prefix = a.Prefix
        LEFT  JOIN TRIMS.ChemicalEnrichment ce ON ce.AnalysisID = ll.AnalysisID
                                               AND ce.MeasurableID = :mid
        WHERE ll.RunID = :rid
        ORDER BY ll.PositionInRun
        """

    rows = conn.execute(
        text(_ef_sql),
        {'rid': run_id} if _measurable_id == 3 else {'rid': run_id, 'mid': _measurable_id}
    ).fetchall()

    for r in rows:
        # Skip rows without Net CPM (should be rare if Step‑3 saved properly)
        if getattr(r, 'MeanCPM', None) is None:
            continue

        mean_cpm = float(r.MeanCPM or 0.0)
        unc_cpm  = float(r.UncCPM or 0.0)
        amt_g    = float(r.SampleAmount or 0.0)
        dil_g    = float(r.SampleDiluent or 0.0)

        is_cpm_net = False  # Default
        try:            
            # Try to get from run protocol snapshot
            run_protocol = ProtocolManager.get_run_protocol_snapshot(run_id, module='LSC')
            if run_protocol:
                for mapping in run_protocol.mappings:
                    if mapping.target_field == 'CPM' and mapping.is_net:
                        is_cpm_net = True
                        break
        except:
            pass

        A_enr, A_enr_unc = _compute_net_activity_dpm_row(
            mean_cpm=mean_cpm,
            unc_cpm=unc_cpm,
            bkg_cpm=0.0 if is_cpm_net else 0.0,  # Already NET in DB at this point
            eff_frac=eff_frac,
            eff_unc_frac=eff_unc_frac,
            sample_amount_g=amt_g,
            sample_diluent_g=dil_g,
            sample_amount_unc_g=vol_unc_g,
            is_net=True  # At this point in the pipeline, CPM is always NET
        )

        # 2) Decay correction from collection date to run_date
        df, df_unc = calculate_decay_factor(r.CollectionDate, run_date)
        A_dpm = A_enr / df if df > 0 else 0.0
        A_dpm_unc = (abs(A_dpm) * (((A_enr_unc/A_enr)**2 + (df_unc/df)**2)**0.5)
                        if abs(A_dpm) > 0 and A_enr > 0 and df > 0 else 0.0)

        # 3) Choose EF — source depends on isotope
        #    3H  : deuterium EF (method 1) > electrolysis spike EF (method 2) > direct (4)
        #    else: chemical enrichment EF (method from ChemicalEnrichment.EnrichmentMethod) > direct (4)
        EF, EF_unc, method_used = 1.0, 0.0, used_method if used_method is not None else 4
        if _measurable_id == 3:
            if getattr(r, 'H2EF', None) not in (None, 0):
                EF = float(r.H2EF or 1.0)
                EF_unc = float(r.H2EFunc or 0.0)
                method_used = 1   # Deuterium
            elif getattr(r, 'EnrichmentFactor', None) not in (None, 0):
                EF = float(r.EnrichmentFactor or 1.0)
                EF_unc = float(r.EnrichmentFactorUnc or 0.0)
                method_used = 2   # Electrolysis spike
        else:
            if getattr(r, 'ChemEF', None) not in (None, 0):
                EF = float(r.ChemEF or 1.0)
                EF_unc = float(r.ChemEFUnc or 0.0)
                method_used = int(r.ChemMeth or 5)  # 5=Gravimetric, 6=SpikeRecovery, 7=Volumetric

        # 4) Pre‑enrichment activity (DPM/kg)
        A_pre = (A_dpm / EF) if EF > 0 else 0.0
        rel_A = (A_dpm_unc / A_dpm) if A_dpm > 0 else 0.0
        rel_E = (EF_unc / EF) if EF > 0 else 0.0
        A_pre_unc = abs(A_pre) * ((rel_A**2 + rel_E**2) ** 0.5) if A_pre != 0 else 0.0

        # 5) Convert to requested unit (TU/Bq/pCi) — base is DPM/kg
        FinalActivity, FinalActivityUnc = convert_activity_unit(
            unit_from=2, unit_to=activity_unit,
            c_value=A_pre, c_unc=A_pre_unc,
            efficiency=eff_frac, efficiency_unc=eff_unc_frac,  # not used for DPM/kg ↔ TU/Bq, but safe
            return_type=1
        )
        FinalActivityUnc = 0 if FinalActivity == 0 or FinalActivityUnc is None else FinalActivityUnc

        # Upsert into TRIMS.LSCResult
        exists = conn.execute(text("""
            SELECT 1 FROM TRIMS.LSCResult WHERE RunID=:rid AND AnalysisID=:aid
        """), {'rid': run_id, 'aid': r.AnalysisID}).scalar()
        activity_fmt, activity_unc_fmt = format_value_uncertainty(FinalActivity, FinalActivityUnc)
        EF_fmt, EF_unc_fmt = format_value_uncertainty(EF, EF_unc)
        params = {
            'rid': run_id,
            'aid': r.AnalysisID,
            'fa': activity_fmt,
            'fu': activity_unc_fmt,
            'unit': activity_unit,
            'ef': EF_fmt,
            'efu': EF_unc_fmt,
            'method': method_used
        }

        if exists:
            conn.execute(text("""
                UPDATE TRIMS.LSCResult
                SET FinalActivity=:fa, FinalActivityUnc=:fu, ActivityUnit=:unit,
                    EnrichmentFactor=:ef, EnrichmentFactorUnc=:efu, EnrichmentFactorMethod=:method
                WHERE RunID=:rid AND AnalysisID=:aid
            """), params)
        else:
            conn.execute(text("""
                INSERT INTO TRIMS.LSCResult
                    (RunID, AnalysisID, FinalActivity, FinalActivityUnc,
                        ActivityUnit, EnrichmentFactor, EnrichmentFactorUnc, EnrichmentFactorMethod)
                VALUES
                    (:rid, :aid, :fa, :fu, :unit, :ef, :efu, :method)
            """), params)

    # conn.commit()

def _bulk_delete_run_mean_and_raw(conn, run_id: int):
    """
    Fast, idempotent cleanup for a run:
      - delete Mean rows (ValueKind=0|1) joined by CountID for this RunID
      - delete Raw rows joined by CountID for this RunID
    Uses SQL Server DELETE ... JOIN syntax.
    """
    # Delete LSCRunMean for this run (Gross/Net)
    conn.execute(text("""
        DELETE FROM TRIMS.LSCRunMean rm
        USING TRIMS.LSCLoadList ll
        WHERE rm.CountID = ll.CountID
          AND ll.RunID = :rid AND rm.ValueKind IN (0, 1)
    """), {'rid': run_id})

    # Delete LSCRunRaw for this run
    conn.execute(text("""
        DELETE FROM TRIMS.LSCRunRaw rr
        USING TRIMS.LSCLoadList ll
        WHERE rr.CountID = ll.CountID
          AND ll.RunID = :rid
    """), {'rid': run_id})



def _get_countid_map(conn, run_id: int) -> dict:
    """
    Returns {PositionInRun -> CountID} for the run to resolve inserts quickly.
    """
    rows = conn.execute(text("""
        SELECT PositionInRun, CountID
        FROM TRIMS.LSCLoadList
        WHERE RunID = :rid
    """), {'rid': run_id}).fetchall()
    return {int(r.PositionInRun): int(r.CountID) for r in rows}

# =============================
# IMPORT DIALOG (Interactive)
# =============================
