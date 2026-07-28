"""
ui/plots_tab.py — Plots tab builder for the IsoWorks SIAM processor window.
Provides helper functions that render scatter, time-series, calibration, and
meteoric water line plots into the processor GUI's output tab widget.
"""

from __future__ import annotations
import os, tempfile, logging
from typing import Dict, List, Tuple, Optional
import pandas as pd

# --------------------- small helpers ---------------------

def _have_cols(df: pd.DataFrame, cols: List[str]) -> bool:
    return isinstance(df, pd.DataFrame) and not df.empty and all(c in df.columns for c in cols)

def _pick_df(*candidates):
    """Return the first non-empty DataFrame; else first DataFrame; else None."""
    for df in candidates:
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df
    for df in candidates:
        if isinstance(df, pd.DataFrame):
            return df
    return None

def _current_isotope(window) -> Optionalstr:
    iso = getattr(window, "current_isotope", None)
    if isinstance(iso, str) and iso.strip():
        return iso.strip()
    try:
        if hasattr(window, "isotope_combo"):
            t = window.isotope_combo.currentText()
            if t: return str(t).strip()
    except Exception as e:

        logging.warning(f"Exception caught: {e}")
    return None

def _current_analysis_id(window):
    aid = None
    try:
        if hasattr(window, "analysis_combo"):
            t = window.analysis_combo.currentText()
            if t:
                tx = str(t).strip()
                aid = int(tx) if tx.isdigit() else tx
    except Exception as e:

        logging.warning(f"Exception caught: {e}")
    return 0 if aid in (None, "") else aid

def _ensure_cumulative_injection(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "cumulative_injection" not in df.columns:
        if "injection_no" in df.columns:
            df["cumulative_injection"] = pd.to_numeric(df["injection_no"], errors="coerce")
        else:
            df["cumulative_injection"] = range(1, len(df)+1)
    return df

def _pair_df(window, x_col: str, y_col: str) -> Optional[pd.DataFrame]:
    """
    Return a DataFrame that has both x_col and y_col.
    Uses window._pair_isotopes_by_sample when available (preferred).
    Falls back to first DataFrame that already contains both columns.
    """
    # Prefer a broad source if one exists (some apps keep a vendor_table around)
    src = getattr(window, "vendor_table", None)
    if not isinstance(src, pd.DataFrame) or src.empty:
        # try analysis_data first (post-calibrated), then data
        src = _pick_df(getattr(window, "analysis_data", None), getattr(window, "data", None))

    # If already wide, done
    if _have_cols(src, [x_col, y_col]):
        return src

    # Try app-provided pairing
    fn = getattr(window, "_pair_isotopes_by_sample", None)
    if callable(fn):
        try:
            paired = fn(src, x_col, y_col)
            if _have_cols(paired, [x_col, y_col]):
                return paired
        except TypeError:
            try:
                paired = fn(x_col, y_col)  # in case method is bound differently
                if _have_cols(paired, [x_col, y_col]):
                    return paired
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    # Naive long->wide pivot fallback (only if a tidy long format is present)
    if isinstance(src, pd.DataFrame) and "sample_id" in src.columns:
        # try to detect a long "isotope" and a value column
        value_cols = [c for c in src.columns if c in ("value","delta","d13C","d15N","d18O","dD","d17O")]
        if "isotope" in src.columns and value_cols:
            val = "value" if "value" in src.columns else "delta" if "delta" in src.columns else value_cols0
            try:
                wide = src.pivot_table(index="sample_id", columns="isotope", values=val, aggfunc="mean").reset_index()
                # merge any meta if needed
                meta = src.drop_duplicates(subset=["sample_id"])
                wide = wide.merge(meta[[c for c in meta.columns if c not in wide.columns] + ["sample_id"]], on="sample_id", how="left")
                if _have_cols(wide, [x_col, y_col]):
                    return wide
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

    return src if isinstance(src, pd.DataFrame) else None

# --------------------- CONFIG BUILDERS ---------------------

def _build_water_configs(window) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    """Return (plotly_cfgs, mpl_cfgs) keyed by unified display names (no suffixes)."""
    cfg_p: Dict[str, dict] = {}
    cfg_m: Dict[str, dict] = {}

    inj_pick = _pick_df(getattr(window, "injection_data", None), getattr(window, "data", None))
    inj = _ensure_cumulative_injection(inj_pick if isinstance(inj_pick, pd.DataFrame) else pd.DataFrame())

    # imports (optional)
    try:
        from plots import plot_water_conc_with_stats_mpl, plot_scatter_mpl, plot_scatter
    except Exception:
        plot_water_conc_with_stats_mpl = None
        plot_scatter_mpl = None
        plot_scatter = None

    # Water conc vs Injection (MPL)
    if plot_water_conc_with_stats_mpl and _have_cols(inj, ["water_conc"]):
        cfg_m["Water Conc. vs Injection"] = {
            "func": plot_water_conc_with_stats_mpl, "args": [], "data_source": "injection", "call_mode": "fig_data_args"
        }

    # Per-isotope vs Injection (MPL)
    for iso in ["d18O", "dD", "d17O"]:
        if plot_scatter_mpl and _have_cols(inj, iso):
            cfg_m[f"{iso} vs Injection"] = {
                "func": plot_scatter_mpl, "args": ["cumulative_injection", iso], "data_source": "injection", "call_mode": "fig_data_args"
            }

    # δ18O vs δD (Interactive) if plotly is available
    try:
        import plotly.graph_objects as go  # noqa: F401
        have_plotly = True
    except Exception:
        have_plotly = False
    if have_plotly and (plot_scatter is not None) and _have_cols(inj, ["d18O","dD"]):
        cfg_p["δ18O vs δD"] = {"func": plot_scatter, "args": ["d18O","dD"], "data_source": "injection"}

    return cfg_p, cfg_m


def _build_irms_ea_configs(window) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    cfg_p: Dict[str, dict] = {}
    cfg_m: Dict[str, dict] = {}

    # MPL scatter with dynamic pairing
    try:
        from plots import plot_scatter_mpl, plot_scatter
    except Exception:
        plot_scatter_mpl = None
        plot_scatter = None

    if plot_scatter_mpl:
        cfg_m["δ13C vs δ15N"] = {
            "func": plot_scatter_mpl, "args": ["d13C","d15N"], "data_source": "analysis",
            "call_mode": "fig_data_args", "dyn_data": (lambda w: _pair_df(w, "d13C","d15N"))
        }
    # Interactive
    try:
        import plotly.graph_objects as go  # noqa: F401
        have_plotly = True
    except Exception:
        have_plotly = False
    if have_plotly and plot_scatter is not None:
        cfg_p["δ13C vs δ15N"] = {
            "func": plot_scatter, "args": ["d13C","d15N"], "data_source": "analysis",
            "dyn_data": (lambda w: _pair_df(w, "d13C","d15N"))
        }

    return cfg_p, cfg_m


def _build_irms_di_configs(window) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    cfg_p: Dict[str, dict] = {}
    cfg_m: Dict[str, dict] = {}

    # MPL scatter (we can build directly or use plot_scatter_mpl if present)
    try:
        from plots import plot_scatter_mpl
    except Exception:
        plot_scatter_mpl = None

    if plot_scatter_mpl:
        cfg_m["δ18O vs δD"] = {
            "func": plot_scatter_mpl, "args": ["d18O","dD"], "data_source": "analysis",
            "call_mode": "fig_data_args", "dyn_data": (lambda w: _pair_df(w, "d18O","dD"))
        }
    else:
        # Fallback simple MPL
        def _mpl_ood(fig, df: pd.DataFrame):
            ax = fig.add_subplot(111)
            ax.scatter(df["d18O"], df["dD"], s=16)
            ax.set_xlabel("δ18O (‰)"); ax.set_ylabel("δD (‰)")
            ax.set_title("δ18O vs δD"); fig.tight_layout(); return fig
        cfg_m["δ18O vs δD"] = {"func": _mpl_ood, "args": [], "data_source": "analysis", "call_mode": "fig_data_args",
                               "dyn_data": (lambda w: _pair_df(w, "d18O","dD"))}

    # Interactive
    try:
        from plots import plot_scatter
        import plotly.graph_objects as go  # noqa: F401
        have_plotly = True
    except Exception:
        plot_scatter = None
        have_plotly = False
    if have_plotly and plot_scatter is not None:
        cfg_p["δ18O vs δD"] = {
            "func": plot_scatter, "args": ["d18O","dD"], "data_source": "analysis",
            "dyn_data": (lambda w: _pair_df(w, "d18O","dD"))
        }

    return cfg_p, cfg_m


# --------------------- PUBLIC API (bind to window) ---------------------

def update_plot_configs(window) -> None:
    """Rebuild and repopulate plot names with unified titles (no suffixes)."""
    window.plot_configs = {}
    window.plot_configs_mpl = {}

    try:
        label = window.instrument_combo.currentText()
    except Exception:
        label = ""

    if label == "IRMS (EA)":
        cfg_p, cfg_m = _build_irms_ea_configs(window)
    elif label == "IRMS (Thermo DI)":
        cfg_p, cfg_m = _build_irms_di_configs(window)
    else:
        cfg_p, cfg_m = _build_water_configs(window)

    window.plot_configs.update(cfg_p)        # interactive / plotly
    window.plot_configs_mpl.update(cfg_m)    # canvas / mpl

    # Unified list of names
    names = []
    seen = set()
    for n in list(cfg_m.keys()) + list(cfg_p.keys()):
        if n not in seen:
            seen.add(n); names.append(n)

    try:
        if hasattr(window, "plot_combo") and window.plot_combo is not None:
            window.plot_combo.blockSignals(True)
            window.plot_combo.clear()
            for name in names:
                window.plot_combo.addItem(name)
    finally:
        try:
            window.plot_combo.blockSignals(False)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")


def _resolve_plot(window, name: str):
    """Return (mpl_cfg, html_cfg) for a given display name."""
    mpl_cfg = window.plot_configs_mpl.get(name)
    html_cfg = window.plot_configs.get(name)
    return mpl_cfg, html_cfg


def _get_plot_data(window, source: str) -> Optional[pd.DataFrame]:
    if source == "raw":
        return getattr(window, "data", None)
    if source == "injection":
        return _pick_df(getattr(window, "injection_data", None), getattr(window, "data", None))
    if source == "analysis":
        return getattr(window, "analysis_data", None)
    if source == "special":
        return getattr(window, "analysis_data", None)
    return getattr(window, "data", None)


def plot_on_canvas(window):
    """Render selected plot with Matplotlib (MPL)."""
    name = window.plot_combo.currentText() if hasattr(window, "plot_combo") else ""
    if not name:
        return
    mpl_cfg, html_cfg = _resolve_plot(window, name)
    cfg = mpl_cfg or html_cfg  # last resort: render html plot as static PNG fallback later
    if cfg is None:
        return

    func = cfg.get("func")
    args = list(cfg.get("args", []))
    dyn = cfg.get("dyn_data") or cfg.get("dyn_args")
    if callable(dyn):
        try:
            dynv = dyn(window)
            if isinstance(dynv, (list, tuple)):
                # dyn_args variant
                args = list(dynv)
            elif isinstance(dynv, pd.DataFrame):
                # dyn_data variant: stash as explicit data
                cfg = dict(cfg)  # shallow copy
                cfg["_explicit_data"] = dynv
        except Exception as _e:
            logging.warning(f"dynamic resolver failed for plot '{name}': {_e}")

    call_mode = cfg.get("call_mode", "fig_data_args")
    data = cfg.get("_explicit_data")
    if data is None:
        data = _get_plot_data(window, cfg.get("data_source", "injection"))

    if call_mode != "fig_args":
        if not isinstance(data, pd.DataFrame) or data.empty:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(window, "No Data", f"No data available for plot '{name}'.")
            return

    # prepare canvas
    try:
        fig = window.figure
        canvas = window.canvas
    except Exception:
        fig = None; canvas = None
    if fig is None:
        from matplotlib.figure import Figure
        fig = Figure(figsize=(6, 4)); window.figure = fig
    else:
        fig.clf()

    try:
        if call_mode == "fig_args":
            res = func(fig, *args)
        else:
            try:
                res = func(fig, data, *args)
                from matplotlib.figure import Figure
                if isinstance(res, Figure):
                    window.figure = res
            except TypeError:
                res = func(data, *args)
                from matplotlib.figure import Figure
                if isinstance(res, Figure):
                    window.figure = res
    except Exception as e:
        logging.error(f"Failed to generate canvas plot '{name}': {e}", exc_info=True)
        from PyQt5.QtWidgets import QMessageBox
        QMessageBox.critical(window, "Plot Error", f"Failed to plot: {e}")
        return

    try:
        if canvas is None and hasattr(window, "plot_area"):
            from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
            canvas = FigureCanvas(window.figure); window.plot_area.layout().addWidget(canvas); window.canvas = canvas
        if canvas is not None:
            canvas.draw_idle()
    except Exception as e:

        logging.warning(f"Exception caught: {e}")


def open_plot_in_browser(window):
    """Render selected plot interactively (Plotly) when available; otherwise, export MPL as an HTML with embedded PNG."""
    name = window.plot_combo.currentText() if hasattr(window, "plot_combo") else ""
    if not name:
        return
    mpl_cfg, html_cfg = _resolve_plot(window, name)
    cfg = html_cfg or mpl_cfg
    if cfg is None:
        return

    func = cfg.get("func")
    args = list(cfg.get("args", []))
    dyn = cfg.get("dyn_data") or cfg.get("dyn_args")
    if callable(dyn):
        try:
            dynv = dyn(window)
            if isinstance(dynv, (list, tuple)):
                args = list(dynv)
            elif isinstance(dynv, pd.DataFrame):
                cfg = dict(cfg); cfg["_explicit_data"] = dynv
        except Exception as _e:
            logging.warning(f"dynamic resolver failed for plot '{name}': {_e}")

    data = cfg.get("_explicit_data")
    if data is None:
        data = _get_plot_data(window, cfg.get("data_source", "injection"))

    outdir = tempfile.gettempdir()
    outfile = os.path.join(outdir, "isotope_plot.html")

    # Try Plotly path
    is_plotly_callable = False
    try:
        import plotly.graph_objects as go  # noqa: F401
        is_plotly_callable = html_cfg is not None  # only treat as plotly if we actually have an html config
    except Exception:
        is_plotly_callable = False

    if is_plotly_callable and callable(func):
        try:
            fig = func(data, *args)
            try:
                html = fig.to_html(include_plotlyjs="cdn", full_html=True)
            except Exception:
                html = fig.to_html(full_html=True)
            with open(outfile, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            logging.error(f"Failed to generate interactive plot '{name}': {e}", exc_info=True)
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.critical(window, "Plot Error", f"Failed to generate interactive plot: {e}")
            return
    else:
        # fallback: export current MPL figure
        try:
            from base64 import b64encode
            from io import BytesIO
            buf = BytesIO()
            if getattr(window, "figure", None) is None:
                plot_on_canvas(window)
            window.figure.savefig(buf, format="png", dpi=150, bbox_inches="tight")
            data_uri = "data:image/png;base64," + b64encode(buf.getvalue()).decode("ascii")
            html = f"<html><body><img src='{data_uri}'/></body></html>"
            with open(outfile, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            logging.error(f"Failed to export MPL plot '{name}': {e}", exc_info=True)
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.critical(window, "Plot Error", f"Failed to export plot: {e}")
            return

    try:
        import webbrowser
        webbrowser.open(f"file://{outfile}")
    except Exception as e:

        logging.warning(f"Exception caught: {e}")


def clear_plots_tab(window):
    """Reset plot state and disable the tab until repopulated."""
    window.plot_configs = {}
    window.plot_configs_mpl = {}

    if hasattr(window, "plot_combo") and window.plot_combo is not None:
        try:
            window.plot_combo.blockSignals(True)
            window.plot_combo.clear()
        finally:
            try: window.plot_combo.blockSignals(False)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

    try:
        if hasattr(window, "figure") and window.figure is not None:
            window.figure.clf()
        if hasattr(window, "canvas") and window.canvas is not None:
            window.canvas.draw_idle()
    except Exception as e:

        logging.warning(f"Exception caught: {e}")

    host = getattr(window, "output_tabs", None) or getattr(window, "tabs", None)
    if host is not None and hasattr(window, "plot_tab"):
        try:
            idx = host.indexOf(window.plot_tab)
            if idx != -1:
                host.setTabEnabled(idx, False)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")


def attach_plots_api(window) -> None:
    """Bind plot functions onto an existing window (and remove overlap with Post tab)."""
    window.update_plot_configs = update_plot_configs.__get__(window)
    window.plot_on_canvas = plot_on_canvas.__get__(window)
    window.open_plot_in_browser = open_plot_in_browser.__get__(window)
    window._get_plot_details = None  # legacy path disabled by this module
    window._clear_plot_tab = clear_plots_tab.__get__(window)
