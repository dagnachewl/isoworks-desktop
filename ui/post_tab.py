"""
ui/post_tab.py — Post-processing tab for the IsoWorks IRMS processor window.
Provides UI builders and renderers for blank diagnostics, carryover candidates,
and control-sample statistics, extracted from the main processor gui module.
"""

# ui/post_tab.py
# Post-Processing tab (IRMS) extracted from gui.py
# Provides UI builders + renderers and a light 'attach' helper to bind into an existing MainWindow instance.

from __future__ import annotations
from typing import Optional
import pandas as pd

from ui.qt_helpers import (
    get_or_create_output_tabs,
    sync_post_tab_visibility,
)
from ui.tables import update_data_table

# Optional diagnostics from isotope_processor; tolerate absence gracefully.
try:
    from isotope_processor import (
        postprocess_irms, IRMSPostConfig,
        compute_blank_diagnostics, compute_carryover_candidates, compute_control_stats
    )
    _HAVE_DIAG = True
except Exception:
    _HAVE_DIAG = False
    # create minimal shims so the module still works without diagnostics imported
    def compute_blank_diagnostics(*args, **kwargs): return pd.DataFrame()
    def compute_carryover_candidates(*args, **kwargs): return pd.DataFrame()
    def compute_control_stats(*args, **kwargs): return pd.DataFrame()


# ---------- Builders ----------

def setup_postprocess_tab(window) -> None:
    """Create the Post-Processing tab with sub-tabs:
        - Results
        - QC Standards
        - Batch Summary
        - Blanks / Carryover
        - Control Stats
    Idempotent: safe to call multiple times.
    Assigns:
        window.post_tab
        window.post_tabs
        window.post_results_table
        window.qc_table
        window.batch_summary_table
        window.blank_diag_table
        window.carryover_table
        window.control_stats_table
    """
    from PyQt5.QtWidgets import QWidget, QVBoxLayout, QTabWidget, QTableWidget, QLabel

    host = get_or_create_output_tabs(window)

    # If already built, ensure it's mounted and return
    if getattr(window, "post_tab", None) is not None:
        try:
            if host.indexOf(window.post_tab) == -1:
                host.addTab(window.post_tab, "Post-Processing")
        finally:
            return

    window.post_tab = QWidget(window)
    outer = QVBoxLayout(window.post_tab)

    window.post_tabs = QTabWidget(window.post_tab)
    outer.addWidget(window.post_tabs)

    # Results
    window.post_results_table = QTableWidget(window.post_tab)
    res_wrap = QWidget(window.post_tab)
    res_layout = QVBoxLayout(res_wrap); res_layout.setContentsMargins(0, 0, 0, 0)
    res_layout.addWidget(window.post_results_table)
    window.post_tabs.addTab(res_wrap, "Results")

    # QC Standards
    window.qc_table = QTableWidget(window.post_tab)
    qc_wrap = QWidget(window.post_tab)
    qc_layout = QVBoxLayout(qc_wrap); qc_layout.setContentsMargins(0, 0, 0, 0)
    qc_layout.addWidget(window.qc_table)
    window.post_tabs.addTab(qc_wrap, "QC Standards")

    # Batch Summary
    window.batch_summary_table = QTableWidget(window.post_tab)
    bs_wrap = QWidget(window.post_tab)
    bs_layout = QVBoxLayout(bs_wrap); bs_layout.setContentsMargins(0, 0, 0, 0)
    bs_layout.addWidget(window.batch_summary_table)
    window.post_tabs.addTab(bs_wrap, "Batch Summary")

    # Blanks / Carryover
    diag_wrap = QWidget(window.post_tab)
    diag_layout = QVBoxLayout(diag_wrap); diag_layout.setContentsMargins(0, 0, 0, 0)
    diag_layout.addWidget(QLabel("Blank diagnostics"))
    window.blank_diag_table = QTableWidget(window.post_tab)
    diag_layout.addWidget(window.blank_diag_table)
    diag_layout.addWidget(QLabel("Top carryover candidates"))
    window.carryover_table = QTableWidget(window.post_tab)
    diag_layout.addWidget(window.carryover_table)
    window.post_tabs.addTab(diag_wrap, "Blanks / Carryover")

    # Control Stats
    window.control_stats_table = QTableWidget(window.post_tab)
    cs_wrap = QWidget(window.post_tab)
    cs_layout = QVBoxLayout(cs_wrap); cs_layout.setContentsMargins(0, 0, 0, 0)
    cs_layout.addWidget(window.control_stats_table)
    window.post_tabs.addTab(cs_wrap, "Control Stats")

    # Mount
    host.addTab(window.post_tab, "Post-Processing")


def reset_postprocess(window, hide_tab: bool = True) -> None:
    """Clear IRMS post-processing state & tables; optionally hide/disable the tab."""
    # state
    window.post_results = None
    window.qc_summary = None
    window.batch_summary = None

    # tables
    try:
        update_data_table(getattr(window, "post_results_table", None), pd.DataFrame())
        update_data_table(getattr(window, "qc_table", None), pd.DataFrame())
        update_data_table(getattr(window, "batch_summary_table", None), pd.DataFrame())
        update_data_table(getattr(window, "blank_diag_table", None), pd.DataFrame())
        update_data_table(getattr(window, "carryover_table", None), pd.DataFrame())
        update_data_table(getattr(window, "control_stats_table", None), pd.DataFrame())
    except Exception as e:

        logging.warning(f"Exception caught: {e}")

    if hide_tab and getattr(window, "post_tab", None) is not None:
        host = get_or_create_output_tabs(window)
        try:
            idx = host.indexOf(window.post_tab)
            if idx != -1:
                host.setTabEnabled(idx, False)
            window.post_tab.setVisible(False)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")


# ---------- Renderer ----------

def update_postprocess_view(window) -> None:
    """Refresh Results / QC / Batch + diagnostics for active isotope."""
    if getattr(window, "post_results_table", None) is None:
        # Build on demand
        setup_postprocess_tab(window)
        reset_postprocess(window, hide_tab=True)

    iso = getattr(window, "current_isotope", None)

    # Results
    res = getattr(window, "post_results", None)
    if isinstance(res, pd.DataFrame) and not res.empty:
        try:
            res_view = window._view_for_isotope(res, iso)
        except Exception:
            res_view = res
    else:
        res_view = pd.DataFrame()
    update_data_table(window.post_results_table, res_view)

    # QC
    qc = getattr(window, "qc_summary", None)
    if isinstance(qc, pd.DataFrame) and not qc.empty:
        qc_view = qc
        if iso and "isotope" in qc_view.columns:
            qc_view = qc_view[qc_view["isotope"].astype(str).str.strip() == iso]
    else:
        qc_view = pd.DataFrame()
    update_data_table(window.qc_table, qc_view)

    # Batch summary
    bs = getattr(window, "batch_summary", None)
    if isinstance(bs, pd.DataFrame) and not bs.empty:
        bs_view = bs
        if iso and "isotope" in bs_view.columns:
            bs_view = bs_view[bs_view["isotope"].astype(str).str.strip() == iso]
    else:
        bs_view = pd.DataFrame()
    update_data_table(window.batch_summary_table, bs_view)

    # Blank diagnostics
    try:
        blanks_df = compute_blank_diagnostics(res, iso if iso else None)
    except Exception:
        blanks_df = pd.DataFrame()
    update_data_table(window.blank_diag_table, blanks_df)

    # Carryover candidates
    try:
        ana = getattr(window, "analysis_data", None)
        if isinstance(ana, pd.DataFrame) and iso and (iso in ana.columns):
            carry_df = compute_carryover_candidates(ana, iso, time_col="timestamp", id_col="sample_id", top_n=30)
        else:
            carry_df = pd.DataFrame()
    except Exception:
        carry_df = pd.DataFrame()
    update_data_table(window.carryover_table, carry_df)

    # Control stats
    try:
        cs_df = compute_control_stats(qc_view if isinstance(qc_view, pd.DataFrame) and not qc_view.empty else qc)
    except Exception:
        cs_df = pd.DataFrame()
    update_data_table(window.control_stats_table, cs_df)

    # Finally, ensure visibility state matches instrument
    try:
        sync_post_tab_visibility(window)
    except Exception as e:

        logging.warning(f"Exception caught: {e}")


# ---------- Convenience wiring ----------

def attach_post_tab_api(window) -> None:
    """Bind post-tab functions as methods on an existing window instance."""
    window.setup_postprocess_tab = setup_postprocess_tab.__get__(window)
    window.reset_postprocess = reset_postprocess.__get__(window)
    # Keep name compatible with existing code (leading underscore)
    window._update_postprocess_view = update_postprocess_view.__get__(window)
