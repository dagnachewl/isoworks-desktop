import logging
import numpy as np
import pandas as pd
from PyQt5.QtCore import QObject, pyqtSignal
from isotope_processor import load_and_prepare_data, process_isotope_data, apply_calibration_generic

class Worker(QObject):
    finished = pyqtSignal(object, object, object, object, object, object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, data_file, standards_data, instrument, config, corrections, roles, methods, isotopes,
                 include_ignored: bool = False, preloaded_df=None):
        
        super().__init__()
        
        self.data_file = data_file
        self.standards_data = standards_data
        self.instrument = instrument
        self.config = config
        self.corrections = corrections or []
        self.roles = roles
        self.methods = methods
        self.isotopes = tuple(isotopes) if isotopes else tuple()
        self.include_ignored = include_ignored
        self.use_generic_calibration = True
        self.calibration_id_col = "sample_id"
        self.preloaded_df = preloaded_df
        
    def _choose_precal_frame(self, prepared_data, injection_data, analysis_data):
        """
        Pick the best frame to calibrate (already corrected for outlier/memory/drift, but not yet calibrated).
        Prefer analysis_data, then injection_data, then prepared_data.
        """
        
        for df in (analysis_data, injection_data, prepared_data):
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
        return None

    def _ensure_id_col(self, df):
        """Ensure we have a 'sample_id' column (rename a common alias if needed)."""
        if df is None:
            return df, self.calibration_id_col
        if self.calibration_id_col in df.columns:
            return df, self.calibration_id_col
        # try a few typical aliases
        for cand in ("Identifier 1", "identifier 1", "Identifier", "identifier", "ID", "id", "sample"):
            if cand in df.columns:
                df = df.copy()
                df.rename(columns={cand: "sample_id"}, inplace=True)
                return df, "sample_id"
        return df, self.calibration_id_col  # may be missing; generic will pass-through

    def _infer_isotopes(self, df):
        """Infer isotopes if none were provided."""
        if self.isotopes:
            return self.isotopes
        if df is None:
            return tuple()
        cols = [str(c) for c in df.columns]
        found = []
        for iso in ("d18O", "dD", "d17O", "d13C", "d15N"):
            if any(c.startswith(iso) for c in cols):
                found.append(iso)
        return tuple(found)

    def _renumber_injections_by_analysis(self, df):
        """
        Renumber injection_no starting at 1 for each LIMS Analysis.
        Stable, deterministic order:
        - prefer existing 'injection_no' (if present) to preserve instrument order
        - else 'timestamp' (if present)
        - else deterministic index order
        """

        if not isinstance(df, pd.DataFrame) or df.empty:
            return df
        if "Analysis" not in df.columns:
            return df

        d = df.copy()

        # pick sort key inside each Analysis
        if "injection_no" in d.columns:
            sort_cols = ["Analysis", "injection_no"]
        elif "Injection" in d.columns:
            sort_cols = ["Analysis", "Injection"]
        elif "timestamp" in d.columns:
            sort_cols = ["Analysis", "timestamp"]
        else:
            d["__ord__"] = np.arange(len(d))
            sort_cols = ["Analysis", "__ord__"]

        d = d.sort_values(sort_cols, kind="mergesort")

        # renumber per LIMS Analysis
        d["injection_no"] = d.groupby("Analysis").cumcount() + 1

        if "__ord__" in d.columns:
            d.drop(columns="__ord__", inplace=True)
        return d

    def run(self):
        try:            
            self.progress.emit("Status: Loading and preparing data...")
            if isinstance(self.preloaded_df, pd.DataFrame) and not self.preloaded_df.empty:
                prepared_data = self.preloaded_df.copy()
                prepared_standards = self.standards_data
            else:
                # Fallback: old path (load + map)
                prepared_data, prepared_standards = load_and_prepare_data(
                    self.data_file, self.standards_data, self.instrument
                )
           
            # LIMS path: we already have Analysis = LIMS AnalysisID
            # Ensure injection_no restarts at 1 for each Analysis
            prepared_data = self._renumber_injections_by_analysis(prepared_data)
                        
            # --- Optionally include ignored injections for Laser data ---
            try:
                from isotope_processor import InstrumentType
                if self.include_ignored and self.instrument in (InstrumentType.LGR, InstrumentType.Picarro):
                    if "ignore" in prepared_data.columns:
                        # Use the isotopes the user selected (fallback to common water cols)
                        isos = [iso for iso in (self.isotopes or []) if iso in prepared_data.columns]
                        if not isos:
                            isos = [c for c in ("d18O", "dD", "d17O") if c in prepared_data.columns]

                        if isos:
                            # Mark rows as include if they have any finite δ-value among selected isotopes
                            has_values = np.isfinite(prepared_data[isos].astype(float)).any(axis=1)
                            mask = (prepared_data["ignore"] != 0) & has_values
                            n_flip = int(mask.sum())
                            if n_flip > 0:
                                prepared_data.loc[mask, "ignore"] = 0
                                self.progress.emit(f"Status: Re-included {n_flip} ignored injections with data.")
            except Exception as _e:

                self.progress.emit("Status: Include-ignored step skipped.")

            self.progress.emit("Status: Processing isotope data...")
            corrections_no_cal = [c for c in (self.corrections or []) if str(c).lower() != "calibration"]
            # Safety: for water instruments, ensure memory/drift are present
            try:
                if str(self.instrument) in ("InstrumentType.LGR", "InstrumentType.Picarro", "LGR", "Picarro"):
                    need = set(["memory", "drift"])
                    have = set(self.corrections or [])
                    if not need.issubset(have):
                        self.corrections = list(have.union(need))
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            injection_data, analysis_data, validation_results, memory_fits, drift_fits, mem_factors = process_isotope_data(
                prepared_data, prepared_standards, self.config,
                corrections_no_cal, self.roles, self.methods, self.isotopes
            )

            # -------- Generic calibration (Laser + IRMS) with stage-aware uncertainties --------
            def _stage_sd(mem_fits, dr_fits, iso):
                s2 = 0.0
                for src in (mem_fits, dr_fits):
                    try:
                        d = src.get(iso, {}) if isinstance(src, dict) else {}
                        sd = float(d.get("sd_resid", 0.0) or 0.0)
                        s2 += sd*sd
                    except Exception as e:

                        logging.warning(f"Exception caught: {e}")
                return float(np.sqrt(s2))

            # choose best pre-cal frame
            pre_cal_df = self._choose_precal_frame(prepared_data, injection_data, analysis_data)
            pre_cal_df, id_col = self._ensure_id_col(pre_cal_df)
            iso_list = self._infer_isotopes(pre_cal_df)

            if pre_cal_df is None or getattr(pre_cal_df, "empty", True) or not iso_list:
                self.progress.emit("Status: Calibration skipped (no data or isotopes).")
                calibrated_df = analysis_data if isinstance(analysis_data, pd.DataFrame) else pre_cal_df
                calibration_fits = {}
            else:
                # collect stage SDs per isotope
                extra_unc = {iso: _stage_sd(memory_fits, drift_fits, iso) for iso in iso_list}
                self.progress.emit("Status: Applying generic calibration...")
                calibrated_df, calibration_fits = apply_calibration_generic(
                    pre_cal_df, prepared_standards, id_col=id_col,
                    isotopes=tuple(iso_list), extra_stage_unc=extra_unc
                )

            analysis_data = calibrated_df
            self.calibration_fits = calibration_fits

            # Emit results (unchanged signature)
            self.finished.emit(injection_data, analysis_data, validation_results, memory_fits, drift_fits, mem_factors)

        except Exception as e:
            logging.error(f"Error in worker thread: {e}", exc_info=True)
            self.error.emit(str(e))
