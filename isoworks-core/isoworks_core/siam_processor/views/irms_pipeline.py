"""
IRMSPipelineMixin
=================
Mixin class that holds IRMS-specific methods for ProcessorWidget.

Usage
-----
    class ProcessorWidget(IRMSPipelineMixin, QWidget):
        ...

All methods use ``self`` exactly as they did when they lived in main_window.py.
No proxy calls, no changed signatures.

Module-level helpers (_iso_norm, _canon_lookup, pair_isotopes_by_sample) are
copied here so the mixin has no imports from main_window (which would be circular).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from PyQt5.QtCore import Qt, QSignalBlocker
from PyQt5.QtWidgets import (
    QComboBox, QLabel, QMessageBox, QSizePolicy, QSpacerItem, QWidget,
)

from isotope_processor import (
    IRMSPostConfig,
    postprocess_irms,
    prepare_ea_single_isotope,
)
from plots import (
    plot_cn_scatter, plot_cn_scatter_mpl,
    plot_combined_fits, plot_combined_fits_mpl,
    plot_drift_fit, plot_drift_fit_mpl,
    plot_drift_standards, plot_drift_standards_mpl,
    plot_irms_calibration, plot_irms_calibration_mpl,
    plot_linearity_standards, plot_linearity_standards_mpl,
    plot_memory_fit, plot_memory_fit_mpl,
    plot_scatter, plot_scatter_mpl,
    plot_timeseries, plot_timeseries_mpl,
    plot_water_conc_with_stats, plot_water_conc_with_stats_mpl,
)
from shared_utils import set_status
from siam_column_resolver import resolve_columns, InstrumentType as ResolverInstrument


# ---------------------------------------------------------------------------
# Module-level pure helpers (also present in main_window.py; no circular dep)
# ---------------------------------------------------------------------------

def _iso_norm(s: str) -> str:
    """Normalize isotope tags (handles 'δ' → 'd')."""
    return str(s or "").replace("δ", "d").strip()


def _canon_lookup(colnames, target):
    """Case-insensitive column lookup; returns the canonical name or None."""
    tl = target.lower().replace(" ", "").replace("_", "")
    for c in colnames:
        if str(c).lower().replace(" ", "").replace("_", "") == tl:
            return c
    return None


def pair_isotopes_by_sample(df, x_col, y_col):
    """
    Build a paired dataset for scatter plots when isotopes are on separate
    rows/sheets (e.g., IRMS DI).
    Strategy:
      1. If rows already have both x_col & y_col → use those rows.
      2. If long layout (has 'isotope' + 'delta') → pivot to wide by sample_id.
      3. Else average per sample_id for each isotope and inner-join.
    Returns DataFrame with at least [x_col, y_col] (and sample_id if available).
    """
    if df is None or getattr(df, "empty", True):
        return df

    # Fast path: paired rows already present
    if x_col in df.columns and y_col in df.columns:
        paired = df[[c for c in df.columns if c in ("sample_id", x_col, y_col)]].dropna(subset=[x_col, y_col])
        if not paired.empty:
            return paired

    cols = set(map(str, df.columns))

    # Long layout: isotope + delta
    if {"isotope", "delta"}.issubset(cols) and "sample_id" in cols:
        tmp = df.copy()
        tmp["iso_norm"] = tmp["isotope"].astype(str).str.strip().str.replace("δ", "d")
        wide = tmp.pivot_table(index="sample_id", columns="iso_norm", values="delta", aggfunc="mean").reset_index()
        if x_col in wide.columns and y_col in wide.columns:
            return wide.dropna(subset=[x_col, y_col])

    # Wide but unpaired: mean per sample_id + join
    if "sample_id" in df.columns:
        parts = []
        for col in (x_col, y_col):
            if col in df.columns:
                parts.append(df[["sample_id", col]].dropna().groupby("sample_id", as_index=False)[col].mean())
        if len(parts) == 2 and not parts[0].empty and not parts[1].empty:
            joined = parts[0].merge(parts[1], on="sample_id", how="inner")
            if not joined.empty:
                return joined

    cols_out = ["sample_id"] if "sample_id" in df.columns else []
    cols_out += [x_col, y_col]
    return pd.DataFrame(columns=cols_out)


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------

class IRMSPipelineMixin:
    """
    IRMS-specific pipeline methods extracted from ProcessorWidget.
    Intended as a mixin only — do not instantiate directly.
    """

    # ------------------------------------------------------------------ #
    # EA peak management                                                   #
    # ------------------------------------------------------------------ #

    def _cfg_ea_peak(self, iso: str, options=None) -> int | None:
        """
        Read preferred EA peak from self.post_cfg (IRMSPostConfig).
        If not set or not in options, return the first option.
        """
        try:
            cfg = getattr(self, "post_cfg", None)
            if cfg and hasattr(cfg, "ea_peak_preference"):
                pref = cfg.ea_peak_preference.get(iso)
                if options and pref in options:
                    return pref
                return options[0] if options else pref
        except Exception as e:
            logging.warning(f"Exception caught: {e}")
        return options[0] if options else None

    def _ea_rebuild_peak_frame(self, isotopes, options_map, chosen_map):
        """Rebuilds 'EA Peak Selection' with one row per isotope: Label | Combo."""
        if not hasattr(self, "ea_peak_group") or not hasattr(self, "ea_peak_layout"):
            return

        lay = self.ea_peak_layout
        self._clear_layout(lay)
        self.ea_peak_combos = {}

        row = 0
        for iso in isotopes or []:
            opts = [int(p) for p in (options_map.get(iso) or []) if p is not None]
            if not opts:
                continue

            lbl = QLabel(f"Peak for {iso}:", self.ea_peak_group)
            lbl.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
            lay.addWidget(lbl, row, 0, alignment=Qt.AlignRight | Qt.AlignVCenter)

            combo = QComboBox(self.ea_peak_group)
            combo.addItems([str(p) for p in sorted(opts)])
            combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            combo.setMaximumHeight(combo.sizeHint().height())

            preferred = None
            try:
                if getattr(self, "post_cfg", None) and hasattr(self.post_cfg, "ea_peak_preference"):
                    preferred = self.post_cfg.ea_peak_preference.get(iso)
            except Exception as e:
                logging.warning(f"Exception caught: {e}")
            if preferred not in opts:
                preferred = chosen_map.get(iso, sorted(opts)[0])
            combo.setCurrentText(str(preferred))

            combo.currentTextChanged.connect(lambda val, _iso=iso: self._ea_on_peak_changed(_iso, val))
            lay.addWidget(combo, row, 1, alignment=Qt.AlignVCenter)
            self.ea_peak_combos[iso] = combo
            lay.setRowStretch(row, 0)
            row += 1

        spacer = QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding)
        lay.addItem(spacer, row, 0, 1, 2)
        lay.setRowStretch(row, 1)
        lay.setColumnStretch(0, 0)
        lay.setColumnStretch(1, 1)
        self.ea_peak_group.setVisible(bool(self.ea_peak_combos))

    def _ea_on_peak_combo_changed_any(self, val: str):
        """Single EA peak combo changed → update per-iso selection and refresh Analysis."""
        if getattr(self, "_ea_peak_updating", False):
            return
        iso = getattr(self, "current_isotope", None)
        if not iso:
            return
        try:
            chosen = int(float(val)) if val and str(val).strip() else None
        except Exception as e:
            logging.warning(f"Exception caught: {e}")
            return
        if chosen is None:
            return
        if not hasattr(self, "_ea_selected_peak_by_iso"):
            self._ea_selected_peak_by_iso = {}
        self._ea_selected_peak_by_iso[iso] = chosen
        try:
            self.on_active_isotope_changed(iso)
        except Exception as e:
            logging.warning(f"Exception caught: {e}")

    def _ea_on_peak_changed(self, iso: str, val: str):
        """Re-filter Analysis for selected isotope/peak. Persist preference and cache per-iso Analysis."""
        try:
            raw_map = getattr(self, "ea_raw_by_iso", {}) or {}
            raw_src = None
            if isinstance(raw_map, dict) and iso in raw_map and isinstance(raw_map[iso], pd.DataFrame):
                raw_src = raw_map[iso]
            else:
                raw_src = getattr(self, "ea_raw_df", None)
            if not isinstance(raw_src, pd.DataFrame) or raw_src.empty:
                return

            chosen = None
            try:
                chosen = int(float(val)) if val and str(val).strip() else None
            except Exception:
                chosen = None

            if not hasattr(self, "post_cfg") or self.post_cfg is None:
                self.post_cfg = IRMSPostConfig()

            _, df_filt, iso2, chosen2, _ = prepare_ea_single_isotope(
                raw_src, isotope=iso, peak=chosen, cfg=self.post_cfg
            )

            try:
                if not hasattr(self.post_cfg, "ea_peak_preference"):
                    self.post_cfg.ea_peak_preference = {}
                self.post_cfg.ea_peak_preference[iso2] = int(chosen2)
            except Exception as e:
                logging.warning(f"Exception caught: {e}")

            if not hasattr(self, "ea_analysis_by_iso"):
                self.ea_analysis_by_iso = {}
            self.ea_analysis_by_iso[iso2] = df_filt.copy(deep=True)

            if getattr(self, "current_isotope", None) == iso2:
                self.analysis_data = df_filt.copy(deep=True)
                self.update_data_table(self.analysis_table, self.analysis_data)
                self.update_plot_configs()

            if hasattr(self, "active_iso_combo"):
                if self.active_iso_combo.findText(iso2) == -1:
                    self.active_iso_combo.addItem(iso2)
                self.active_iso_combo.setCurrentText(iso2)
                self.current_isotope = iso2

            self.status_label.setText(f"Status: EA {iso2} peak = {chosen2}")
        except Exception as e:
            logging.warning(f"EA peak selection change failed: {e}")

    def _ea_force_single_peak_combo(self):
        """
        Keep only one EA peak selector (self.ea_peak_combo).
        If an 'analysis' combo exists, disconnect and destroy it.
        Idempotent.
        """
        try:
            cb2 = getattr(self, "ea_peak_combo_analysis", None)
            if cb2 and isinstance(cb2, QWidget):
                try:
                    cb2.currentTextChanged.disconnect()
                except Exception as e:
                    logging.warning(f"Exception caught: {e}")
                try:
                    lay = cb2.parentWidget().layout() if cb2.parentWidget() else None
                    if lay:
                        lay.removeWidget(cb2)
                except Exception as e:
                    logging.warning(f"Exception caught: {e}")
                try:
                    cb2.setVisible(False)
                except Exception as e:
                    logging.warning(f"Exception caught: {e}")
                try:
                    cb2.deleteLater()
                except Exception as e:
                    logging.warning(f"Exception caught: {e}")
                try:
                    self.ea_peak_combo_analysis = None
                except Exception as e:
                    logging.warning(f"Exception caught: {e}")

            cb = getattr(self, "ea_peak_combo", None)
            if cb:
                try:
                    cb.setVisible(True)
                except Exception as e:
                    logging.warning(f"Exception caught: {e}")
            self._ea_single_peak_combo = True
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Raw data freeze / per-isotope access                                #
    # ------------------------------------------------------------------ #

    def _freeze_ea_raw(self):
        """Remember pristine EA raw so later steps cannot overwrite it."""
        try:
            if isinstance(getattr(self, "data", None), pd.DataFrame) and not self.data.empty:
                self.ea_raw_df = self.data.copy(deep=True)
        except Exception as e:
            logging.warning(f"Exception caught: {e}")

    def _raw_df_for_isotope(self, iso: str):
        """Return the per-isotope RAW dataframe; fall back to self.data."""
        iso = _iso_norm(iso)
        try:
            if isinstance(getattr(self, "ea_raw_by_iso", None), dict):
                df = self.ea_raw_by_iso.get(iso)
                if isinstance(df, pd.DataFrame):
                    return df.copy()
        except Exception as e:
            logging.warning(f"Exception caught: {e}")
        try:
            if isinstance(getattr(self, "di_raw_by_iso", None), dict):
                df = self.di_raw_by_iso.get(iso)
                if isinstance(df, pd.DataFrame):
                    return df.copy()
        except Exception as e:
            logging.warning(f"Exception caught: {e}")
        try:
            df = getattr(self, "data", None)
            return df.copy() if hasattr(df, "copy") else pd.DataFrame()
        except Exception as e:
            logging.warning(f"Exception caught: {e}")
            return pd.DataFrame()

    def _identify_isotopes_in_df(self, df: pd.DataFrame) -> list[str]:
        """Return list of isotopes present in a vendor DataFrame."""
        cols = {c.lower() for c in df.columns}
        hits = []
        if "d13c" in cols:
            hits.append("d13C")
        if "d15n" in cols:
            hits.append("d15N")
        if "d18o" in cols:
            hits.append("d18O")
        if "dd" in cols or "d2h" in cols:
            hits.append("dD")
        if "d17o" in cols:
            hits.append("d17O")
        if "isotope" in cols:
            more = df["isotope"].astype(str).str.strip().str.replace("δ", "d").unique().tolist()
            for m in more:
                if m and m not in hits:
                    hits.append(m)
        return hits

    def _view_for_isotope(self, df, iso=None):
        """Return a readable slice of df for the active isotope."""
        if not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()

        meta_candidates = [
            "sample_id", "ref_name", "role_code", "timestamp", "injection_no",
            "cumulative_injection", "water_conc", "block_no", "Analysis",
            "is_outlier", "ignore",
        ]
        cols = list(df.columns)

        if not iso or not isinstance(iso, str) or not any(
            str(c).lower().startswith(iso.lower()) for c in cols
        ):
            meta = [c for c in meta_candidates if c in cols]
            rest = [c for c in cols if c not in meta]
            return df[meta + rest].copy()

        meta = [c for c in meta_candidates if c in cols]
        iso_lower = iso.lower()
        iso_cols = [c for c in cols if str(c).lower().startswith(iso_lower)]
        extras = [e for e in ("d_excess", "17O_Excess") if e in cols]

        ordered = meta + [c for c in cols if c in iso_cols] + extras
        seen: set = set()
        ordered = [c for c in ordered if not (c in seen or seen.add(c))]

        if not iso_cols:
            return df[meta + [c for c in cols if c not in meta]].copy()
        return df[ordered].copy()

    # ------------------------------------------------------------------ #
    # Post-processing helpers                                              #
    # ------------------------------------------------------------------ #

    def _get_active_analysis_frame(self):
        """Return the analysis DataFrame for the current isotope/instrument."""
        iso = getattr(self, "current_isotope", None)
        inst = self._active_instrument_label()
        if "IRMS (EA)" in inst:
            dct = getattr(self, "multi_iso_analysis", None)
            if isinstance(dct, dict) and iso in dct:
                return dct[iso]
        if "IRMS (Thermo DI)" in inst or "Thermo DI" in inst or "DI" in inst:
            dct = getattr(self, "di_analysis_by_iso", None)
            if isinstance(dct, dict) and iso in dct:
                return dct[iso]
        return getattr(self, "analysis_data", None)

    def _post_cache_dict(self):
        """Get the dict used to cache per-isotope post-processing results."""
        inst = self._active_instrument_label()
        if "IRMS (EA)" in inst:
            if not hasattr(self, "ea_post_by_iso"):
                self.ea_post_by_iso = {}
            return self.ea_post_by_iso
        if "IRMS (Thermo DI)" in inst or "Thermo DI" in inst or "DI" in inst:
            if not hasattr(self, "di_post_by_iso"):
                self.di_post_by_iso = {}
            return self.di_post_by_iso
        return None

    def _refresh_post_for_active_iso(self, force_recompute: bool = False):
        """Ensure the Post-Processing tab shows QC for the current isotope."""
        try:
            if not self._ensure_post_ui():
                return
        except Exception as e:
            logging.warning(f"Exception caught: {e}")
            return

        ana = self._get_active_analysis_frame()
        if not isinstance(ana, pd.DataFrame) or ana.empty:
            self.post_results = pd.DataFrame()
            self.qc_summary = {}
            self.batch_summary = {}
            try:
                self._sync_post_tab_visibility()
                self._update_postprocess_view()
            except Exception as e:
                logging.warning(f"Exception caught: {e}")
            return

        cache = self._post_cache_dict()
        iso = getattr(self, "current_isotope", None)
        cfg = getattr(self, "post_cfg", None)
        if cfg is None:
            try:
                cfg = IRMSPostConfig()
                self.post_cfg = cfg
            except Exception as e:
                logging.warning(f"Exception caught: {e}")

        if cache is not None and not force_recompute and iso in cache:
            snap = cache[iso]
            self.post_results = snap.get("results")
            self.qc_summary = snap.get("qc")
            self.batch_summary = snap.get("batch")
            try:
                self._sync_post_tab_visibility()
                self._update_postprocess_view()
            except Exception as e:
                logging.warning(f"Exception caught: {e}")
            return

        try:
            if cfg is None:
                cfg = IRMSPostConfig()
            res, qc, bs = postprocess_irms(ana, cfg)
            self.post_results, self.qc_summary, self.batch_summary = res, qc, bs
            if cache is not None and iso:
                cache[iso] = {"results": res, "qc": qc, "batch": bs}
            try:
                self._sync_post_tab_visibility()
                self._update_postprocess_view()
            except Exception as e:
                logging.warning(f"Exception caught: {e}")
        except Exception as e:
            logging.debug(f"Post refresh failed: {e}")

    def _sync_post_tab_visibility(self):
        """Enable/disable the Post tab based on instrument & data presence."""
        host = self._tab_host()
        if host is None or not hasattr(self, "post_tab"):
            return
        idx = host.indexOf(self.post_tab)
        if idx == -1:
            return
        label = self.instrument_combo.currentText() if hasattr(self, "instrument_combo") else ""
        is_irms = label in ("IRMS (EA)", "IRMS (Thermo DI)")
        has_analysis = isinstance(getattr(self, "analysis_data", None), pd.DataFrame) and not self.analysis_data.empty
        host.setTabEnabled(idx, bool(is_irms and has_analysis))

    # ------------------------------------------------------------------ #
    # Plot configuration                                                   #
    # ------------------------------------------------------------------ #

    def _get_plot_details(self):
        """Resolve selected plot into (data_to_use, config, mpl_config, plot_name, sample_ids)."""
        plot_name = self.plot_combo.currentText()
        if not plot_name:
            return None, None, None, None, []

        config = self.plot_configs.get(plot_name)
        mpl_config = self.plot_configs_mpl.get(plot_name)
        if config is None or mpl_config is None:
            QMessageBox.warning(self, "Plot Error", f"No configuration found for plot: {plot_name}")
            return None, None, None, None, []

        data_source_name = config.get("data_source")
        data_to_use = None
        sample_ids = []

        if data_source_name == "raw":
            data_to_use = self.data
        elif data_source_name == "injection":
            data_to_use = self.injection_data if getattr(self, "injection_data", None) is not None else self.data
        elif data_source_name == "analysis":
            data_to_use = self.analysis_data
        elif data_source_name == "special":
            isotope = self.get_active_isotope()
            analysis_id_str = self.analysis_combo.currentText() if hasattr(self, "analysis_combo") else ""

            if not isotope:
                QMessageBox.warning(self, "Selection Missing", "Please select an isotope.")
                return None, None, None, None, []

            analysis_id = analysis_id_str
            if plot_name in ["Memory Fit", "Combined Fit Diagnostics"]:
                if self._memory_model_needs_analysis():
                    if not analysis_id_str:
                        QMessageBox.warning(self, "Selection Missing", "Please select an analysis ID.")
                        return None, None, None, None, []
                    try:
                        analysis_id = int(analysis_id_str) if str(analysis_id_str).isdigit() else analysis_id_str
                    except Exception as e:
                        logging.warning(f"Exception caught: {e}")

            if plot_name == "Memory Fit":
                data_to_use = self.memory_fits
                config["args"] = mpl_config["args"] = [isotope, analysis_id]
                sample_ids = self._get_sample_ids_for_plot("is_memory_id", isotope)
            elif plot_name == "Drift Fit":
                data_to_use = (self.drift_fits, self.analysis_data)
                config["args"] = mpl_config["args"] = [isotope]
                sample_ids = self._get_sample_ids_for_plot("is_drift_id", isotope)
            elif plot_name == "Combined Fit Diagnostics":
                data_to_use = (self.memory_fits, self.drift_fits, self.analysis_data)
                config["args"] = mpl_config["args"] = [isotope, analysis_id]
                sample_ids = self._get_sample_ids_for_plot("is_memory_id", isotope)
            else:
                QMessageBox.warning(self, "Plot Error", f"Unknown 'special' plot: {plot_name}")
                return None, None, None, None, []
        else:
            QMessageBox.warning(self, "Plot Error", f"Unknown data source: {data_source_name}")
            return None, None, None, None, []

        if data_to_use is None:
            QMessageBox.warning(self, "No Data", f"Required data '{data_source_name}' is not available.")
            return None, None, None, None, []

        # Auto-pair isotopes for IRMS (Thermo DI) scatter plots
        try:
            instrument_label = self.instrument_combo.currentText()
        except Exception:
            instrument_label = ""

        if (
            instrument_label == "IRMS (Thermo DI)"
            and data_source_name in ("raw", "injection", "analysis")
            and isinstance(data_to_use, pd.DataFrame)
        ):
            try:
                args = config.get("args", [])
                if isinstance(args, (list, tuple)) and len(args) == 2:
                    x_col, y_col = args[0], args[1]
                    need_pair = True
                    if {x_col, y_col}.issubset(data_to_use.columns):
                        try:
                            need_pair = data_to_use[[x_col, y_col]].dropna().empty
                        except Exception:
                            need_pair = True
                    if need_pair:
                        paired = pair_isotopes_by_sample(data_to_use, x_col, y_col)
                        if paired is not None and not getattr(paired, "empty", True):
                            data_to_use = paired
            except Exception as e:
                logging.warning(f"Exception caught: {e}")

        return data_to_use, config, mpl_config, plot_name, sample_ids

    def update_plot_configs(self, preserve_selection: bool = True):
        prev_plot = self._remember_plot_selection() if preserve_selection else ""
        self.plot_configs = {}
        self.plot_configs_mpl = {}

        def _truthy(obj):
            if obj is None:
                return False
            if isinstance(obj, dict):
                return any(_truthy(v) for v in obj.values())
            if isinstance(obj, (pd.DataFrame, pd.Series)):
                return not obj.empty
            if isinstance(obj, np.ndarray):
                return obj.size > 0
            if hasattr(obj, "__len__") and not isinstance(obj, (str, bytes)):
                try:
                    return len(obj) > 0
                except Exception as e:
                    logging.warning(f"Exception caught: {e}")
                    return True
            return True

        mem_ok = _truthy(getattr(self, "memory_fits", None))
        drf_ok = _truthy(getattr(self, "drift_fits", None))

        if mem_ok:
            self.plot_configs["Memory Fit"] = {"func": plot_memory_fit, "args": [], "data_source": "special"}
            self.plot_configs_mpl["Memory Fit"] = {"func": plot_memory_fit_mpl, "args": [], "data_source": "special"}
        if drf_ok:
            self.plot_configs["Drift Fit"] = {"func": plot_drift_fit, "args": [], "data_source": "special"}
            self.plot_configs_mpl["Drift Fit"] = {"func": plot_drift_fit_mpl, "args": [], "data_source": "special"}
        if mem_ok and drf_ok:
            self.plot_configs["Combined Fit Diagnostics"] = {"func": plot_combined_fits, "args": [], "data_source": "special"}
            self.plot_configs_mpl["Combined Fit Diagnostics"] = {"func": plot_combined_fits_mpl, "args": [], "data_source": "special"}

        try:
            if getattr(self, "data", None) is not None:
                self.data = resolve_columns(self.data, instrument=ResolverInstrument.IRMS_EA)
            if getattr(self, "analysis_data", None) is not None:
                self.analysis_data = resolve_columns(self.analysis_data, instrument=ResolverInstrument.IRMS_EA)
        except Exception as _e:
            logging.debug(f"Column resolution for EA skipped: {_e}")

        if self.data is not None and "water_conc" in self.data.columns:
            self.plot_configs["Water Conc. vs Injection"] = {"func": plot_water_conc_with_stats, "args": [], "data_source": "injection"}
            self.plot_configs_mpl["Water Conc. vs Injection"] = {"func": plot_water_conc_with_stats_mpl, "args": [], "data_source": "injection"}

        if self.data is not None:
            if "d18O" in self.data.columns and "dD" in self.data.columns:
                self.plot_configs["d18O vs dD (Raw)"] = {"func": plot_scatter, "args": ["d18O", "dD"], "data_source": "raw"}
                self.plot_configs_mpl["d18O vs dD (Raw)"] = {"func": plot_scatter_mpl, "args": ["d18O", "dD"], "data_source": "raw"}
            if "d17O" in self.data.columns and "d18O" in self.data.columns:
                self.plot_configs["d18O vs d17O (Raw)"] = {"func": plot_scatter, "args": ["d18O", "d17O"], "data_source": "raw"}
                self.plot_configs_mpl["d18O vs d17O (Raw)"] = {"func": plot_scatter_mpl, "args": ["d18O", "d17O"], "data_source": "raw"}
            for iso in ("d18O", "dD", "d17O"):
                if iso in self.data.columns:
                    self.plot_configs[f"Time Series {iso} (Raw)"] = {"func": plot_timeseries, "args": [iso], "data_source": "raw"}
                    self.plot_configs_mpl[f"Time Series {iso} (Raw)"] = {"func": plot_timeseries_mpl, "args": [iso], "data_source": "raw"}

        if self.analysis_data is not None:
            if "d18O_calibrated" in self.analysis_data.columns and "dD_calibrated" in self.analysis_data.columns:
                self.plot_configs["d18O vs dD (Processed)"] = {"func": plot_scatter, "args": ["d18O_calibrated", "dD_calibrated"], "data_source": "analysis"}
                self.plot_configs_mpl["d18O vs dD (Processed)"] = {"func": plot_scatter_mpl, "args": ["d18O_calibrated", "dD_calibrated"], "data_source": "analysis"}
            if (
                "d17O_calibrated" in self.analysis_data.columns
                and "d18O_calibrated" in self.analysis_data.columns
            ):
                self.plot_configs["d18O vs d17O (Processed)"] = {"func": plot_scatter, "args": ["d18O_calibrated", "d17O_calibrated"], "data_source": "analysis"}
                self.plot_configs_mpl["d18O vs d17O (Processed)"] = {"func": plot_scatter_mpl, "args": ["d18O_calibrated", "d17O_calibrated"], "data_source": "analysis"}
            if "d_excess" in self.analysis_data.columns and "d18O_calibrated" in self.analysis_data.columns:
                self.plot_configs["d-excess vs d18O (Processed)"] = {"func": plot_scatter, "args": ["d18O_calibrated", "d_excess"], "data_source": "analysis"}
                self.plot_configs_mpl["d-excess vs d18O (Processed)"] = {"func": plot_scatter_mpl, "args": ["d18O_calibrated", "d_excess"], "data_source": "analysis"}
            if "17O_Excess" in self.analysis_data.columns and "d18O_calibrated" in self.analysis_data.columns:
                self.plot_configs["d18O vs 17O-Excess (Processed)"] = {"func": plot_scatter, "args": ["d18O_calibrated", "17O_Excess"], "data_source": "analysis"}
                self.plot_configs_mpl["d18O vs 17O-Excess (Processed)"] = {"func": plot_scatter_mpl, "args": ["d18O_calibrated", "17O_Excess"], "data_source": "analysis"}
            if self.injection_data is not None:
                for col in self.injection_data.columns:
                    if isinstance(col, str) and col.endswith("_memory_corrected"):
                        iso_name = col.replace("_memory_corrected", "")
                        self.plot_configs[f"Time Series {iso_name} (Processed)"] = {"func": plot_timeseries, "args": [col], "data_source": "injection"}
                        self.plot_configs_mpl[f"Time Series {iso_name} (Processed)"] = {"func": plot_timeseries_mpl, "args": [col], "data_source": "injection"}

        def has_cn(df):
            return df is not None and not getattr(df, "empty", True) and {"d13C", "d15N"}.issubset(set(df.columns))

        if has_cn(getattr(self, "data", None)):
            self.plot_configs["δ13C vs δ15N (Raw)"] = {"func": plot_cn_scatter, "args": [], "data_source": "raw", "kwargs": {}}
            self.plot_configs_mpl["δ13C vs δ15N (Raw)"] = {"func": plot_cn_scatter_mpl, "args": [], "data_source": "raw", "kwargs": {}}

        if has_cn(getattr(self, "analysis_data", None)):
            self.plot_configs["δ13C vs δ15N (Analysis)"] = {"func": plot_cn_scatter, "args": [], "data_source": "analysis", "kwargs": {}}
            self.plot_configs_mpl["δ13C vs δ15N (Analysis)"] = {"func": plot_cn_scatter_mpl, "args": [], "data_source": "analysis", "kwargs": {}}

            try:
                diag_df = getattr(self, "analysis_data", None) or getattr(self, "data", None)
                if isinstance(diag_df, pd.DataFrame) and not diag_df.empty:
                    amount_col = next((c for c in ("Amount", "Area", "Peak Area", "PeakArea") if c in diag_df.columns), None)
                    if amount_col:
                        for iso in ("d13C", "d15N"):
                            if iso in diag_df.columns or f"{iso}_calibrated" in diag_df.columns:
                                self.plot_configs[f"Linearity (standards) {iso} — Raw"] = {"func": plot_linearity_standards, "args": [iso], "data_source": "analysis", "kwargs": {"y_col": iso, "amount_col": amount_col}}
                                self.plot_configs_mpl[f"Linearity (standards) {iso} — Raw"] = {"func": plot_linearity_standards_mpl, "args": [iso], "data_source": "analysis", "kwargs": {"y_col": iso, "amount_col": amount_col}}
                                if f"{iso}_calibrated" in diag_df.columns:
                                    self.plot_configs[f"Linearity (standards) {iso} — Calibrated"] = {"func": plot_linearity_standards, "args": [iso], "data_source": "analysis", "kwargs": {"y_col": f"{iso}_calibrated", "amount_col": amount_col}}
                                    self.plot_configs_mpl[f"Linearity (standards) {iso} — Calibrated"] = {"func": plot_linearity_standards_mpl, "args": [iso], "data_source": "analysis", "kwargs": {"y_col": f"{iso}_calibrated", "amount_col": amount_col}}

                    x_col = next((c for c in ("injection_no", "timestamp") if c in diag_df.columns), None)
                    if x_col:
                        for iso in ("d13C", "d15N"):
                            if iso in diag_df.columns or f"{iso}_calibrated" in diag_df.columns:
                                self.plot_configs[f"Drift (standards) {iso} vs {x_col} — Raw"] = {"func": plot_drift_standards, "args": [iso], "data_source": "analysis", "kwargs": {"y_col": iso, "x_col": x_col}}
                                self.plot_configs_mpl[f"Drift (standards) {iso} vs {x_col} — Raw"] = {"func": plot_drift_standards_mpl, "args": [iso], "data_source": "analysis", "kwargs": {"y_col": iso, "x_col": x_col}}
                                if f"{iso}_calibrated" in diag_df.columns:
                                    self.plot_configs[f"Drift (standards) {iso} vs {x_col} — Calibrated"] = {"func": plot_drift_standards, "args": [iso], "data_source": "analysis", "kwargs": {"y_col": f"{iso}_calibrated", "x_col": x_col}}
                                    self.plot_configs_mpl[f"Drift (standards) {iso} vs {x_col} — Calibrated"] = {"func": plot_drift_standards_mpl, "args": [iso], "data_source": "analysis", "kwargs": {"y_col": f"{iso}_calibrated", "x_col": x_col}}
            except Exception as _e:
                logging.debug(f"Adding IRMS diagnostics plots skipped: {_e}")

            if getattr(self, "calibration_fits", None) and getattr(self, "analysis_data", None) is not None:
                cur_iso = getattr(self, "current_isotope", None)
                if cur_iso and cur_iso in self.analysis_data.columns and f"{cur_iso}_true" in self.analysis_data.columns:
                    name = f"Calibration Fit ({cur_iso})"
                    self.plot_configs[name] = {"func": plot_irms_calibration, "args": [], "data_source": "analysis", "kwargs": {"isotope": cur_iso, "fits": self.calibration_fits}}
                    self.plot_configs_mpl[name] = {"func": plot_irms_calibration_mpl, "args": [], "data_source": "analysis", "kwargs": {"isotope": cur_iso, "fits": self.calibration_fits}}

        try:
            if isinstance(self.analysis_data, pd.DataFrame) and not self.analysis_data.empty:
                for iso in ("d15N", "d13C"):
                    if iso in self.analysis_data.columns:
                        self.plot_configs[f"Time Series {iso} (Analysis)"] = {"func": plot_timeseries, "args": [iso], "data_source": "analysis"}
                        self.plot_configs_mpl[f"Time Series {iso} (Analysis)"] = {"func": plot_timeseries_mpl, "args": [iso], "data_source": "analysis"}
        except Exception as e:
            logging.warning(f"Exception caught: {e}")

        try:
            self.plot_combo.blockSignals(True)
            self.plot_combo.clear()
            self.plot_combo.addItems(list(self.plot_configs.keys()))
        finally:
            self.plot_combo.blockSignals(False)

        if preserve_selection and prev_plot:
            self._restore_plot_selection(prev_plot)

        try:
            has_plots = len(self.plot_configs) > 0
            idx = self.output_tabs.indexOf(self.plot_tab)
            if idx != -1:
                self.output_tabs.setTabEnabled(idx, has_plots)
            set_status(self.status_label, f"Status: {len(self.plot_configs)} plot option(s) available.", "success")
        except Exception as _e:
            logging.debug(f"Plot tab/label update skipped: {_e}")

    # ------------------------------------------------------------------ #
    # Input panel helpers                                                  #
    # ------------------------------------------------------------------ #

    def _ensure_ea_option_controls(self):
        """Create EA (IRMS) option combos once."""
        if not hasattr(self, "ea_linearity_combo"):
            self.ea_linearity_combo = QComboBox(self.input_panel)
            self.ea_linearity_combo.addItems(["Off", "Linear", "Quadratic"])
        if not hasattr(self, "ea_drift_combo"):
            self.ea_drift_combo = QComboBox(self.input_panel)
            self.ea_drift_combo.addItems(["Off", "Linear (Time)", "Linear (Order)"])

    # ------------------------------------------------------------------ #
    # Table display helpers                                                #
    # ------------------------------------------------------------------ #

    def _freeze_sample_id(self, table, df, id_candidates=("sample_id", "ref_name")):
        """Show the id column as frozen row header; hide it from the body."""
        try:
            if df is None or getattr(df, "empty", True):
                return
            cols = [str(c) for c in df.columns]
            id_col = next((c for c in id_candidates if c in cols), None)
            if not id_col:
                return
            col_idx = cols.index(id_col)
            series = df[id_col].astype(str).str.split("/").str[0].str.strip()
            labels = [v if v else "" for v in series]
            table.verticalHeader().setVisible(True)
            table.setVerticalHeaderLabels(labels)
            table.verticalHeader().setMinimumSectionSize(22)
            table.verticalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            table.verticalHeader().setStretchLastSection(False)
            table.setStyleSheet("QTableWidget QHeaderView::section { padding-left: 6px; }")
            table.setColumnHidden(col_idx, True)
        except Exception as e:
            logging.debug(f"Freeze sample_id skipped: {e}")
