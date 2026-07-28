"""
plots.py — Plotting library for IsoWorks isotope data visualisation.
Generates scatter, time-series, drift/memory fit, calibration, and meteoric
water line plots using both Plotly (interactive HTML) and Matplotlib (static).
"""
from __future__ import annotations
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from scipy.stats import linregress
import logging
import os
import re
import math
import io
import base64
from jinja2 import Template
from matplotlib.figure import Figure
from matplotlib.gridspec import GridSpec
import matplotlib.dates as mdates

def _apply_datetime_xaxis(ax) -> None:
    """
    Configure a matplotlib axis with auto-adapting date tick labels.

    Uses AutoDateLocator + ConciseDateFormatter so that tick density and
    label format are re-evaluated on every draw (including after resize),
    preventing label overlap without manual rotation hacks.
    """
    locator = mdates.AutoDateLocator(minticks=3, maxticks=6)
    formatter = mdates.ConciseDateFormatter(locator)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)
    # Slight rotation as a final safety net for very narrow canvases
    ax.tick_params(axis='x', labelrotation=20)
    for lbl in ax.get_xticklabels():
        lbl.set_ha('right')


# Meteoric Water Line functions
def GMWL_Craig(d18O): return 8.0 * d18O + 10
def GMWL_Gat(d18O): return 8.20 * d18O + 11.27
def GardMWL(d18O): return 7.4 * d18O + 7.3
def HerMWL(d18O): return 8.0 * d18O + 13.5
def MMWL(d18O): return 8 * d18O + 22
def GMWL_17O(delta_18O):
    """
    Calculate δ¹⁷O based on δ¹⁸O using the relationship:
    ln(δ¹⁷O + 1) = 0.528 * ln(δ¹⁸O + 1) + 0.000033
    
    Parameters:
    delta_18O (float or array): The δ¹⁸O value (in per mil)
    
    Returns:
    float or array: The calculated δ¹⁷O value (in per mil), or NaN if input is invalid
    """
    # Ensure input is valid for log (delta_18O + 1 > 0)
    delta_18O = np.asarray(delta_18O)
    valid = delta_18O + 1 > 0
    result = np.full_like(delta_18O, np.nan, dtype=float)

    # Compute only for valid inputs
    ln_term = 0.528 * np.log(delta_18O[valid] + 1) + 0.000033
    result[valid] = np.exp(ln_term) - 1
    return result

mwl_functions = {"GMWL_Craig": GMWL_Craig, "GMWL_Gat": GMWL_Gat, "GardMWL": GardMWL, "HerMWL": HerMWL, "MMWL": MMWL, "GMWL_17O" : GMWL_17O}

def _mask_non_linearity(df: pd.DataFrame) -> pd.Series:
    """
    Return a boolean mask selecting rows that are NOT marked as linearity standards.
    If 'role' is missing, return all True.
    Treats labels like 'Linearity' or role codes like 'AMLIN' as linearity.
    """
    if not isinstance(df, pd.DataFrame) or "role_code" not in df.columns:
        return pd.Series(True, index=df.index)  # no role -> keep all
    r = df["role_code"].astype(str).str.strip().str.lower()
    return ~r.isin({"linearity", "amlin"})

# --- Function restored ---
def _is_plottable(df):
    return isinstance(df, pd.DataFrame) and not df.empty

def _prepare_plot_data(data: pd.DataFrame, required_cols: list):
    if data is None or data.empty: return None
    plot_data = data.copy()
    if 'cumulative_injection' not in plot_data.columns:
        plot_data['cumulative_injection'] = range(1, len(plot_data) + 1)
    for col in ['d17O', '17O_Excess', 'd_excess']:
        if col not in plot_data.columns: plot_data[col] = np.nan
    if not all(col in plot_data.columns for col in required_cols):
        missing = [col for col in required_cols if col not in plot_data.columns]
        raise KeyError(f"Plotting data is missing required columns: {missing}")
    plot_data.dropna(subset=required_cols, inplace=True)
    if plot_data.empty: return None
    return plot_data

def plot_scatter(data, x_col, y_col, output_file=None, mwl=None, sample_ids=None):
    logging.info(f"Generating Plotly scatter plot: {x_col} vs {y_col}")
    plot_data = _prepare_plot_data(data, [x_col, y_col])
    fig = go.Figure()
    if plot_data is None: fig.add_annotation(text="No data available to plot.", showarrow=False); return fig

    # --- FINAL FIX: Explicitly check for roles and outliers before filtering ---
    has_roles = 'role' in plot_data.columns
    is_outlier_col = 'outlier_count' if 'outlier_count' in plot_data.columns else 'is_outlier'
    has_outliers = is_outlier_col in plot_data.columns and plot_data[is_outlier_col].sum() > 0
    outliers = plot_data[plot_data[is_outlier_col] > 0] if has_outliers else pd.DataFrame()
    if has_roles:
        # outliers = plot_data[plot_data[is_outlier_col] > 0] if has_outliers else pd.DataFrame()
        non_outliers = plot_data[~(plot_data[is_outlier_col] > 0)] if has_outliers else plot_data
        samples = non_outliers[non_outliers['role'].isin(['Sample'])]
        standards = non_outliers[~non_outliers['role'].isin(['Sample'])]
        
        if not samples.empty:
            fig.add_trace(go.Scatter(
                x=samples[x_col],
                y=samples[y_col],
                mode='markers',
                name='Samples',
                marker=dict(color='green', size=8),
                customdata=samples['sample_id'],
                hovertemplate=f"{x_col}: %{{x:.1f}}<br>" f"{y_col}: %{{y}}<br>" "Sample ID: %{customdata}<extra></extra>"))
        
        if not standards.empty: fig.add_trace(go.Scatter(x=standards[x_col], y=standards[y_col], mode='markers', name='Standards & Controls', marker=dict(color='blue', size=8), customdata=standards['sample_id'],
                                                         hovertemplate=f"{x_col}: %{{x:.1f}}<br>" f"{y_col}: %{{y}}<br>" "Sample ID: %{customdata}<extra></extra>"))
    else: # Raw data with no roles assigned yet
        fig.add_trace(go.Scatter(x=plot_data[x_col], y=plot_data[y_col], mode='markers', name='Data', marker=dict(color='green', size=8), customdata=plot_data['sample_id'],
                                 hovertemplate=f"{x_col}: %{{x:.1f}}<br>" f"{y_col}: %{{y}}<br>" "Sample ID: %{customdata}<extra></extra>"))

    if not outliers.empty: fig.add_trace(go.Scatter(x=outliers[x_col], y=outliers[y_col], mode='markers', name='Outliers', marker=dict(color='red', symbol='x', size=10), customdata=outliers['sample_id'],
                                                    hovertemplate=f"{x_col}: %{{x:.1f}}<br>" f"{y_col}: %{{y}}<br>" "Sample ID: %{customdata}<extra></extra>"))

    mwl_to_plot = data.get('mwl_applied', pd.Series(mwl)).iloc[0] if 'mwl_applied' in data.columns else mwl
    
    if y_col in ['d17O', 'd17O_calibrated'] and x_col in ['d18O', 'd18O_calibrated']:
        mwl_to_plot = "GMWL_17O"
        x_range = np.linspace(plot_data[x_col].min(), plot_data[x_col].max(), 100)
        y_range = GMWL_17O(x_range / 1000) * 1000
        fig.add_trace(go.Scatter(x=x_range, y=y_range, mode='lines', name='ln(δ¹⁷O + 1) = 0.528 * ln(δ¹⁸O + 1) + 0.000033', line=dict(color='red', dash='dash'), customdata=plot_data['sample_id'],
                                 hovertemplate='X: %{x:.1f}</b> Y: %{y} <br> Sample ID: %{customdata}</i>'))
        fig.update_layout(title=f'{x_col} vs {y_col}', xaxis_title='δ¹⁸O (‰)', yaxis_title='δ¹⁷O (‰)', template='plotly_white',
                              legend=dict(
        x=0.02,  # Position legend near top-left corner (inside plot)
        y=0.98,
        xanchor='left',
        yanchor='top',
        bgcolor='rgba(255, 255, 255, 0.5)',  # Semi-transparent background
        bordercolor='black',
        borderwidth=1
    ))
        #fig.update_layout(title=f'{x_col} vs {y_col}', xaxis_title=x_col.replace('_',' ').title(), yaxis_title=y_col.replace('_',' ').title(), template='plotly_white')
    elif y_col in ['dD', 'dD_calibrated'] and x_col in ['d18O', 'd18O_calibrated'] and mwl_to_plot in mwl_functions:
        x_range = np.linspace(plot_data[x_col].min(), plot_data[x_col].max(), 100)
        y_range = mwl_functions[mwl_to_plot](x_range)
        fig.add_trace(go.Scatter(x=x_range, y=y_range, mode='lines', name=mwl_to_plot, line=dict(color='black', dash='dash')))
    else:
        fig.update_layout(title=f'{x_col} vs {y_col}', xaxis_title=x_col.replace('_',' ').title(), yaxis_title=y_col.replace('_',' ').title(), template='plotly_white')
    if output_file: fig.write_html(output_file); logging.info(f"Plot saved to {output_file}")
    return fig

def plot_water_conc_with_stats(data, output_file=None, mwl=None, sample_ids=None):
    logging.info("Generating Plotly water concentration plot with statistics.")
    plot_data = _prepare_plot_data(data, ['water_conc', 'cumulative_injection'])
    fig = go.Figure()
    if plot_data is None: fig.add_annotation(text="No data to plot.", showarrow=False); return fig
    fig = plot_scatter(plot_data, 'cumulative_injection', 'water_conc')
    is_outlier_col = 'outlier_count' if 'outlier_count' in plot_data.columns else 'is_outlier'
    stats_data = plot_data[plot_data[is_outlier_col] == 0] if is_outlier_col in plot_data.columns else plot_data
    mask = _mask_non_linearity(stats_data)
    try:
        series = pd.to_numeric(stats_data.loc[mask, 'water_conc'], errors='coerce')
    except Exception:
        series = pd.to_numeric(plot_data['water_conc'], errors='coerce')        
    if not stats_data.empty:
        mean_val = float(series.mean())
        std_val  = float(series.std())    
        # mean_val, std_val = stats_data['water_conc'].mean(), stats_data['water_conc'].std()
        # mean_val, std_val = stats_data[stats_data['role']!='Linearity']['water_conc'].mean(), stats_data[stats_data['role']!='Linearity']['water_conc'].std()
        upper, lower = mean_val + 2 * std_val, mean_val - 2 * std_val
        fig.add_hline(y=mean_val, line_dash="solid", line_color="red", annotation_text=f"Mean") #: {mean_val:.2f}")
        fig.add_hline(y=upper, line_dash="dash", line_color="orange", annotation_text=f"Mean + 2σ") #: {upper:.2f}")
        fig.add_hline(y=lower, line_dash="dash", line_color="orange", annotation_text=f"Mean - 2σ") #: {lower:.2f}")
    fig.update_layout(title_text="Water Concentration with Mean & 2σ Bands", template='plotly_white')
    if output_file: fig.write_html(output_file); logging.info(f"Plot saved to {output_file}")
    return fig

def plot_timeseries(data, y_col, output_file=None, mwl=None, sample_ids=None):
    logging.info(f"Generating Plotly time series plot for {y_col}")
    x_axis_col = 'timestamp'
    plot_data = _prepare_plot_data(data, [x_axis_col, y_col])
    fig = go.Figure()
    if plot_data is None: fig.add_annotation(text="No data to plot.", showarrow=False); return fig
    plot_data.sort_values(x_axis_col, inplace=True)
    is_outlier_col = 'outlier_count' if 'outlier_count' in plot_data.columns else 'is_outlier'
    has_outliers = is_outlier_col in plot_data.columns and plot_data[is_outlier_col].sum() > 0
    has_roles = 'role' in plot_data.columns
    outliers = plot_data[plot_data[is_outlier_col] > 0] if has_outliers else pd.DataFrame()
    non_outliers = plot_data[~(plot_data[is_outlier_col] > 0)] if has_outliers else plot_data
    
    if not non_outliers.empty:
        fig.add_trace(go.Scatter(x=non_outliers[x_axis_col], y=non_outliers[y_col], mode='lines', line=dict(color='lightgrey'), showlegend=False))
        
    if has_roles:
        samples = non_outliers[non_outliers['role'].isin(['Sample', 'Standard'])]
        standards = non_outliers[~non_outliers['role'].isin(['Sample', 'Standard'])]
        if not samples.empty: fig.add_trace(go.Scatter(x=samples[x_axis_col], y=samples[y_col], mode='markers', name='Samples', marker=dict(color='green', size=7)))
        if not standards.empty: fig.add_trace(go.Scatter(x=standards[x_axis_col], y=standards[y_col], mode='markers', name='Standards & Controls', marker=dict(color='blue', size=7)))
    else:
        if not non_outliers.empty: fig.add_trace(go.Scatter(x=non_outliers[x_axis_col], y=non_outliers[y_col], mode='markers', name='Data', marker=dict(color='green', size=7)))
        
    if not outliers.empty:
        fig.add_trace(go.Scatter(x=outliers[x_axis_col], y=outliers[y_col], mode='markers', name='Outliers', marker=dict(color='red', symbol='x', size=10)))

    fig.update_layout(title=f'Time Series for {y_col}', xaxis_title=x_axis_col.replace('_', ' ').title(), yaxis_title=y_col, template='plotly_white')
    if output_file: fig.write_html(output_file); logging.info(f"Plot saved to {output_file}")
    return fig

def _add_memory_fit_traces(fig, fit_data, isotope, analysis_id, row=None, col=None):
    # Defaults (2R intra-sample)
    title = f'Intra-Sample Memory Fit for {isotope} - Analysis {analysis_id}'
    y_axis_title = isotope
    kwargs = {'row': row, 'col': col} if row and col else {}

    # If standards-characterized (analysis_id==0), original defaults:
    if analysis_id == 0:
        title = f'Standard-Characterized Two-Pool Memory Fit for {isotope}'
        y_axis_title = "Memory Fraction"

    if not fit_data:
        fig.add_annotation(
            text=f"No memory fit data available for {isotope}, Analysis {analysis_id}.",
            showarrow=False, **kwargs
        )
        return title

    # Raw points
    fig.add_trace(
        go.Scatter(x=fit_data['x_raw'], y=fit_data['y_raw'],
                   mode='markers', name='Raw Data / Fractions'),
        **kwargs
    )

    # --- 1-POOL OVERRIDE (Single Reservoir) ---
    model = str(fit_data.get("model", "")).lower()
    is_one_pool = model in ("1-pool", "one-pool", "single-pool", "1r")

    if is_one_pool:
        # Clear, unambiguous text for 1R
        if analysis_id == 0:
            title = f'Standard-Characterized Single-Pool Memory Fit for {isotope}'
            y_axis_title = "Memory Fraction"
        else:
            title = f'Single-Pool Memory Fit for {isotope}'
        # 1R uses a single exponential fit line
        x_fit = fit_data.get("x_fit")
        y_fit = fit_data.get("y_fit_total", fit_data.get("y_fit"))
        if x_fit is not None and y_fit is not None:
            fig.add_trace(
                go.Scatter(x=x_fit, y=y_fit, mode='lines',
                           name='Single-Pool Fit', line=dict(color='red')),
                **kwargs
            )
    else:
        # Original 2R rendering (fast / slow / combined)
        fig.add_trace(
            go.Scatter(x=fit_data['x_fit'], y=fit_data.get('y_fit_fast'),
                       mode='lines', name='Fast Component',
                       line=dict(color='orange', dash='dot')),
            **kwargs
        )
        fig.add_trace(
            go.Scatter(x=fit_data['x_fit'], y=fit_data.get('y_fit_slow'),
                       mode='lines', name='Slow Component',
                       line=dict(color='purple', dash='dot')),
            **kwargs
        )
        fig.add_trace(
            go.Scatter(x=fit_data['x_fit'], y=fit_data.get('y_fit_total'),
                       mode='lines', name='Combined Fit', line=dict(color='red')),
            **kwargs
        )

    # Axes / labels
    if row and col:
        fig.update_xaxes(title_text='Injection Number', row=row, col=col)
        fig.update_yaxes(title_text=y_axis_title, row=row, col=col)
    else:
        fig.update_layout(xaxis_title='Injection Number', yaxis_title=y_axis_title, template='plotly_white')

    return title


def _add_drift_fit_traces(fig, fit_data, isotope, row=None, col=None):
    kwargs = {'row': row, 'col': col} if row and col else {}
    fit_params, data = fit_data
    if not fit_params:
        fig.add_annotation(text=f"No drift fit data available for {isotope}.", showarrow=False, **kwargs); return
    drift_data = fit_params['data']; popt = fit_params['popt']
    uncorrected_col = f'{isotope}_memory_corrected'; corrected_col = f'{isotope}_drift_corrected'
    fig.add_trace(go.Scatter(x=drift_data['timestamp'], y=drift_data[uncorrected_col], mode='markers', name='Before Correction', marker_color='blue'), **kwargs)
    fig.add_trace(go.Scatter(x=drift_data['timestamp'], y=drift_data[corrected_col], mode='markers', name='After Correction', marker_color='green'), **kwargs)
    x_fit_sec = np.linspace(drift_data['seconds'].min(), drift_data['seconds'].max(), 100)
    y_fit_orig = popt[0] * x_fit_sec + popt[1]
    x_fit_time = pd.to_datetime(data['timestamp'].min()) + pd.to_timedelta(x_fit_sec, unit='s')
    fig.add_trace(go.Scatter(x=x_fit_time, y=y_fit_orig, mode='lines', name='Original Drift Fit', line=dict(color='red', dash='dash')), **kwargs)
    corrected_fit = linregress(x=drift_data['seconds'], y=drift_data[corrected_col])
    y_fit_corr = corrected_fit.slope * x_fit_sec + corrected_fit.intercept
    fig.add_trace(go.Scatter(x=x_fit_time, y=y_fit_corr, mode='lines', name=f'Corrected Fit (Slope: {corrected_fit.slope:.2e})', line=dict(color='green')), **kwargs)
    if row and col:
        fig.update_xaxes(title_text='Timestamp', row=row, col=col)
        fig.update_yaxes(title_text=isotope, row=row, col=col)
    else:
        fig.update_layout(xaxis_title='Timestamp', yaxis_title=isotope, template='plotly_white')
    
def plot_memory_fit(memory_fits, isotope, analysis_id, output_file=None, mwl=None, sample_ids=None):
    fig = go.Figure()
    fit_data = memory_fits.get(isotope, {}).get(analysis_id)
    title = _add_memory_fit_traces(fig, fit_data, isotope, analysis_id)
    fig.update_layout(title=title, width=700, height=600, template='plotly_white')
    if output_file: fig.write_html(output_file)
    return fig

def plot_drift_fit(fit_data, isotope, output_file=None, mwl=None, sample_ids=None):
    drift_fits, data = fit_data
    fig = go.Figure()
    fit_params = drift_fits.get(isotope)
    _add_drift_fit_traces(fig, (fit_params, data), isotope)
    fig.update_layout(title=f'Drift Correction for {isotope}', template='plotly_white')
    if output_file: fig.write_html(output_file)
    return fig

def plot_combined_fits(fit_data, isotope, analysis_id, output_file=None, mwl=None, sample_ids=None):
    memory_fits, drift_fits, data = fit_data
    logging.info(f"Generating combined diagnostics plot for {isotope}, Analysis {analysis_id}")
    fig = make_subplots(rows=1, cols=2, subplot_titles=(f'Drift Correction for {isotope}', 'Memory Correction'), row_heights=[0.4]) #, 0.4])
    #_add_drift_fit_traces(fig, drift_fits.get(isotope), isotope, row=1, col=1)
    _add_drift_fit_traces(fig, (drift_fits.get(isotope), data), isotope, row=1, col=1)
    mem_title = _add_memory_fit_traces(fig, memory_fits.get(isotope, {}).get(analysis_id), isotope, analysis_id, row=1, col=2)
    fig.layout.annotations[1].update(text=mem_title)
    fig.update_layout(height=900, title_text=f"Fit Diagnostics for {isotope}", template='plotly_white')
    if output_file: fig.write_html(output_file)
    return fig
    
# --- Matplotlib Functions ---

def plot_scatter_mpl(figure, data, x_col, y_col, mwl=None, sample_ids=None):
    logging.info(f"Generating Matplotlib scatter plot: {x_col} vs {y_col}")
    figure.clear()
    
    if y_col in ['d17O', 'd17O_calibrated', 'dD', 'dD_calibrated'] and x_col in ['d18O', 'd18O_calibrated']:
        ax = figure.add_subplot(1, 2, 1)
    else:
        ax = figure.add_subplot(111)
    
    plot_data = _prepare_plot_data(data, [x_col, y_col])
    if plot_data is None: 
        ax.text(0.5, 0.5, "No data available to plot.", ha='center', va='center')
        return

    is_outlier_col = 'outlier_count' if 'outlier_count' in plot_data.columns else 'is_outlier'
    has_outliers = is_outlier_col in plot_data.columns and plot_data[is_outlier_col].sum() > 0
    has_roles = 'role' in plot_data.columns
    outliers = plot_data[plot_data[is_outlier_col] > 0] if has_outliers else pd.DataFrame()
    non_outliers = plot_data[~(plot_data[is_outlier_col] > 0)] if has_outliers else plot_data
    
    if has_roles:
        samples = non_outliers[non_outliers['role'].isin(['Sample'])]
        standards = non_outliers[~non_outliers['role'].isin(['Sample'])]
        if not samples.empty:
            ax.scatter(samples[x_col], samples[y_col], c='green', label='Samples', s=20)
        if not standards.empty:
            ax.scatter(standards[x_col], standards[y_col], c='blue', label='Standards & Controls', s=20)
    else:
        ax.scatter(non_outliers[x_col], non_outliers[y_col], c='green', label='Data', s=20)

    if not outliers.empty: ax.scatter(outliers[x_col], outliers[y_col], c='red', label='Outliers', marker='x', s=50)

    mwl_to_plot = data.get('mwl_applied', pd.Series(mwl)).iloc[0] if 'mwl_applied' in data.columns else mwl

    if y_col in ['d17O', 'd17O_calibrated'] and x_col in ['d18O', 'd18O_calibrated']:
        mwl_to_plot = "GMWL_17O"
        x_range = np.linspace(plot_data[x_col].min(), plot_data[x_col].max(), 100)
        y_range = GMWL_17O(x_range / 1000) * 1000
        ax.plot(x_range, y_range, 'k--', label='ln(δ¹⁷O + 1) = 0.528 * ln(δ¹⁸O + 1) + 0.000033')
        ax.set_xlabel('δ¹⁸O (‰)')
        ax.set_ylabel('δ¹⁷O (‰)')
        ax.set_title('δ¹⁸O vs δ¹⁷O')
        if 'd17O_calibrated' in data.columns and '17O_Excess' in data.columns:
            ax2 = figure.add_subplot(1, 2, 2)
            ax2.scatter(samples[x_col], samples['17O_Excess'], c='green', label='Samples', s=20)
            ax2.set_xlabel('δ¹⁸O (‰)')
            ax2.set_ylabel('17O_Excess')
            ax2.set_title(f'{x_col} vs 17O_Excess')
            ax2.legend()
            ax2.grid(True)            
    elif y_col in ['dD', 'dD_calibrated'] and x_col in ['d18O', 'd18O_calibrated']:
        if mwl_to_plot in mwl_functions:
            x_range = np.linspace(plot_data[x_col].min(), plot_data[x_col].max(), 100)
            y_range = mwl_functions[mwl_to_plot](x_range)
            ax.plot(x_range, y_range, 'k--', label=mwl_to_plot)
        ax.set_xlabel('δ¹⁸O (‰)')
        ax.set_ylabel('δD (‰)')
        ax.set_title('δ¹⁸O vs δD')
        if 'd_excess' in data.columns and x_col == 'd18O_calibrated' and y_col == 'dD_calibrated':
            ax2 = figure.add_subplot(1, 2, 2)
            ax2.scatter(samples[x_col], samples['d_excess'], c='green', label='Samples', s=20)
            ax2.set_xlabel('δ¹⁸O (‰)')
            ax2.set_ylabel('d_excess')
            ax2.set_title(f'{x_col} vs d_excess')
            ax2.legend()
            ax2.grid(True)
    else:
        ax.set_title(f'{x_col} vs {y_col}')
        ax.set_xlabel(x_col.replace('_', ' ')) #.title())
        ax.set_ylabel(y_col.replace('_', ' ')) #.title())
    ax.legend()
    ax.grid(True)

def plot_water_conc_with_stats_mpl(figure, data, mwl=None, sample_ids=None):
    logging.info("Generating Matplotlib water concentration plot.")
    figure.clear()
    ax = figure.add_subplot(111)
    plot_data = _prepare_plot_data(data, ['water_conc', 'cumulative_injection'])
    if plot_data is None: 
        ax.text(0.5, 0.5, "No data available to plot.", ha='center', va='center')
        return
        
    plot_scatter_mpl(figure, plot_data, 'cumulative_injection', 'water_conc')
    ax = figure.axes[0]
    is_outlier_col = 'outlier_count' if 'outlier_count' in plot_data.columns else 'is_outlier'
    stats_data = plot_data[plot_data[is_outlier_col] == 0] if is_outlier_col in plot_data.columns else plot_data
    # Stats source (prefer original to access 'role' if present)
    mask = _mask_non_linearity(stats_data)
    try:
        series = pd.to_numeric(stats_data.loc[mask, 'water_conc'], errors='coerce')
    except Exception:
        series = pd.to_numeric(plot_data['water_conc'], errors='coerce')
        
    if not stats_data.empty:
        mean_val = float(series.mean())
        std_val  = float(series.std())
        # mean_val, std_val = stats_data[stats_data['role']!='Linearity']['water_conc'].mean(), stats_data[stats_data['role']!='Linearity']['water_conc'].std()
        upper, lower = mean_val + 2 * std_val, mean_val - 2 * std_val
        ax.axhline(mean_val, color='red', linestyle='-', label=f"Mean") #: {mean_val:.2f}")
        ax.axhline(upper, color='orange', linestyle='--', label=f"Mean + 2σ") #: {upper:.2f}")
        ax.axhline(lower, color='orange', linestyle='--', label=f"Mean - 2σ") #: {lower:.2f}")
    
    ax.set_title("Water Concentration with Mean & 2σ Bands")
    ax.legend()

def plot_timeseries_mpl(figure, data, y_col, mwl=None, sample_ids=None):
    logging.info(f"Generating Matplotlib time series plot for {y_col}")
    figure.clear()
    ax = figure.add_subplot(111)
    x_axis_col = 'timestamp'
    plot_data = _prepare_plot_data(data, [x_axis_col, y_col])
    if plot_data is None: 
        ax.text(0.5, 0.5, "No data available to plot.", ha='center', va='center')
        return
        
    plot_data.sort_values(x_axis_col, inplace=True)
    is_outlier_col = 'outlier_count' if 'outlier_count' in plot_data.columns else 'is_outlier'
    has_outliers = is_outlier_col in plot_data.columns and plot_data[is_outlier_col].sum() > 0
    has_roles = 'role' in plot_data.columns
    outliers = plot_data[plot_data[is_outlier_col] > 0] if has_outliers else pd.DataFrame()
    non_outliers = plot_data[~(plot_data[is_outlier_col] > 0)] if has_outliers else plot_data
    
    if not non_outliers.empty:
        ax.plot(non_outliers[x_axis_col], non_outliers[y_col], color='lightgrey', zorder=1)
        
    if has_roles:
        samples = non_outliers[non_outliers['role'].isin(['Sample', 'Standard'])]
        standards = non_outliers[~non_outliers['role'].isin(['Sample', 'Standard'])]
        if not samples.empty: ax.scatter(samples[x_axis_col], samples[y_col], c='green', label='Samples', s=15, zorder=2)
        if not standards.empty: ax.scatter(standards[x_axis_col], standards[y_col], c='blue', label='Standards & Controls', s=15, zorder=2)
    else:
        if not non_outliers.empty: ax.scatter(non_outliers[x_axis_col], non_outliers[y_col], c='green', label='Data', s=15, zorder=2)
        
    if not outliers.empty:
        ax.scatter(outliers[x_axis_col], outliers[y_col], c='red', marker='x', s=40, label='Outliers', zorder=3)

    ax.set_title(f'Time Series for {y_col}')
    ax.set_xlabel(x_axis_col.replace('_', ' ').title())
    ax.set_ylabel(y_col)
    ax.legend()
    ax.grid(True)
    figure.autofmt_xdate()

def plot_memory_fit_mpl(figure, memory_fits, isotope, analysis_id, mwl=None, sample_ids=None):
    """
    Robust Memory-Fit plotter:
      - Two-pool (fast/slow): 'y_fit_fast'/'y_fit_slow' (+ 'y_fit_total')
      - One-pool:             'y_fit_total' (or 'pred')
      - Exponential/Asympt.:  'y_fit' (or 'pred')
    Also plots 'x_raw'/'y_raw' if available. Silently falls back to analysis_id 0 or first key.
    """

    def _get(fd, *keys):
        # Safe getter that doesn't use 'or' with arrays
        for k in keys:
            if k in fd and fd[k] is not None:
                return fd[k]
        return None

    logging.info(f"Generating Matplotlib memory fit plot for {isotope}, Analysis {analysis_id}")
    figure.clear()
    ax = figure.add_subplot(111)

    # --- fetch fit dict safely (prefer requested id → 0 → first entry)
    iso_dict = (memory_fits or {}).get(isotope, {})
    fit_data = None
    if isinstance(iso_dict, dict) and iso_dict:
        # coerce analysis_id to int if possible
        try:
            aid = int(float(analysis_id)) if analysis_id not in (None, "") else None
        except Exception:
            aid = None
        if aid in iso_dict:
            fit_data = iso_dict[aid]
            analysis_id = aid
        elif 0 in iso_dict:
            fit_data = iso_dict[0]
            analysis_id = 0
        else:
            # first available key
            first_key = next(iter(iso_dict.keys()))
            fit_data = iso_dict[first_key]
            analysis_id = first_key

    if not fit_data:
        ax.text(0.5, 0.5, "No memory fit data available.", ha='center', va='center')
        ax.set_axis_off()
        return

    # --- pull raw points if present
    x_raw = _get(fit_data, "x_raw")
    y_raw = _get(fit_data, "y_raw")
    try:
        if x_raw is not None and y_raw is not None:
            xr = np.asarray(x_raw, dtype=float)
            yr = np.asarray(y_raw, dtype=float)
            if xr.size and yr.size:
                ax.plot(xr, yr, "o", label="Raw / Fractions")
    except Exception as e:

        logging.warning(f"Exception caught: {e}")

    # --- detect model and plot fitted curves
    model = str(_get(fit_data, "model") or "").lower()

    # X for fitted curves
    x_fit = _get(fit_data, "x_fit", "x")
    if x_fit is None and x_raw is not None:
        x_fit = np.linspace(np.nanmin(x_raw), np.nanmax(x_raw), 200)
    xf = np.asarray(x_fit, dtype=float) if x_fit is not None else None

    plotted_any = False

    if "2" in model and "pool" in model:
        y_fast  = _get(fit_data, "y_fit_fast")
        y_slow  = _get(fit_data, "y_fit_slow")
        y_total = _get(fit_data, "y_fit_total")
        if xf is not None and y_fast is not None:
            ax.plot(xf, np.asarray(y_fast, dtype=float), "--", label="Fast Component"); plotted_any = True
        if xf is not None and y_slow is not None:
            ax.plot(xf, np.asarray(y_slow, dtype=float), "--", label="Slow Component"); plotted_any = True
        if xf is not None and y_total is not None:
            ax.plot(xf, np.asarray(y_total, dtype=float), "-", label="Combined Fit", linewidth=2); plotted_any = True
    elif "1" in model and "pool" in model or "single" in model:
        y_tot = _get(fit_data, "y_fit_total", "y_fit", "pred")
        if xf is not None and y_tot is not None:
            ax.plot(xf, np.asarray(y_tot, dtype=float), "-", label="Single-Pool Fit", linewidth=2); plotted_any = True
    else:
        # generic exp/asymptotic: expect 'y_fit' or 'pred'
        y_any = _get(fit_data, "y_fit", "y_fit_total", "pred")
        if xf is not None and y_any is not None:
            ax.plot(xf, np.asarray(y_any, dtype=float), "-", label="Fit", linewidth=2); plotted_any = True

    # --- titles/labels
    title_iso = isotope or ""
    if "2" in model and "pool" in model:
        title = f"Two-Pool Memory Fit for {title_iso}"
        ylab  = "Memory Fraction"
    elif "1" in model and "pool" in model or "single" in model:
        title = f"Single-Pool Memory Fit for {title_iso}"
        ylab  = "Memory Fraction"
    else:
        title = f"Memory Fit for {title_iso}"
        ylab  = title_iso or "Value"


    if sample_ids:
        title += f" [IDs: {', '.join(map(str, sample_ids))}]"

    ax.set_title(title)
    ax.set_xlabel("Injection Number")
    ax.set_ylabel(ylab)
    if plotted_any:
        ax.legend()
    ax.grid(True)

    # integer ticks if the range is small/numeric
    try:
        Xmin = None; Xmax = None
        if x_raw is not None:
            arr = np.asarray(x_raw, dtype=float); 
            if arr.size: Xmin = np.nanmin(arr); Xmax = np.nanmax(arr)
        if xf is not None and np.size(xf):
            Xmin = np.nanmin(xf) if Xmin is None else min(Xmin, np.nanmin(xf))
            Xmax = np.nanmax(xf) if Xmax is None else max(Xmax, np.nanmax(xf))
        if Xmin is not None and Xmax is not None and np.isfinite([Xmin, Xmax]).all() and (Xmax - Xmin) <= 50:
            ax.set_xticks(np.arange(np.floor(Xmin), np.ceil(Xmax) + 1, 1))
    except Exception as e:

        logging.warning(f"Exception caught: {e}")


def _draw_drift_ax(ax, drift_fits, data, isotope, sample_ids = None):
    """
    Draws the drift plot on the provided axis, identical to plot_drift_fit_mpl.
    Returns True if drawn, False if no data.
    """
    fit_params = drift_fits.get(isotope)
    if not fit_params:
        ax.text(0.5, 0.5, "No drift fit data available.", ha='center', va='center')
        ax.set_title(f'Drift Correction for {isotope}')
        ax.set_xlabel('Timestamp')
        ax.set_ylabel(isotope)
        ax.grid(True)
        return False

    drift_data = fit_params['data']
    popt = fit_params['popt']

    uncorrected_col = f'{isotope}_memory_corrected'
    corrected_col   = f'{isotope}_drift_corrected'

    # Scatter: before vs after (same as your first function)
    ax.plot(drift_data['timestamp'], drift_data[uncorrected_col], 'o', color='blue',  label='Before Correction')
    ax.plot(drift_data['timestamp'], drift_data[corrected_col],   'o', color='green', label='After Correction')

    # Fit lines (same construction as in your first function)
    x_fit_sec  = np.linspace(0, drift_data['seconds'].max(), 100)  # start from 0 for consistency
    y_fit_orig = popt[0] * x_fit_sec + popt[1]
    # Important: anchor the fit time to the global data['timestamp'].min() for consistency
    x_fit_time = pd.to_datetime(data['timestamp'].min()) + pd.to_timedelta(x_fit_sec, unit='s')
    ax.plot(x_fit_time, y_fit_orig, '--', color='red', label='Original Drift Fit')

    corrected_fit = linregress(x=drift_data['seconds'], y=drift_data[corrected_col])
    y_fit_corr = corrected_fit.slope * x_fit_sec + corrected_fit.intercept
    ax.plot(x_fit_time, y_fit_corr, '-', color='green',
            label=f'Corrected Fit (Slope: {corrected_fit.slope:.2e})')

    # Cosmetics
    title = f"Drift Correction for {isotope}"
    if sample_ids:
        title += f" [IDs: {', '.join(map(str, sample_ids))}]"
    ax.set_title(title)
    ax.set_xlabel('Time')
    ax.set_ylabel(isotope)
    ax.legend()
    ax.grid(True)
    _apply_datetime_xaxis(ax)

    return True


# ---------------------------------------------
# standalone drift plot
# ---------------------------------------------
def plot_drift_fit_mpl(figure, fit_data, isotope, mwl=None, sample_ids=None):
    logging.info(f"Generating Matplotlib drift fit plot for {isotope}")
    figure.clear()
    drift_fits, data = fit_data

    ax = figure.add_subplot(111)
    _draw_drift_ax(ax, drift_fits, data, isotope, sample_ids)
    figure.set_tight_layout(True)


# ---------------------------------------------------
# Combined: drift (same as above) + memory diagnostics
# ---------------------------------------------------
def plot_combined_fits_mpl(figure: Figure, fit_data: tuple, isotope: str, analysis_id: int, mwl: str = None, sample_ids=None) -> None:
    """
    - Drift panel now uses the same logic as plot_drift_fit_mpl for consistency.
    - Layout is adaptive:
        * If the canvas is wide enough (>= 9 inches), arrange side-by-side (1x2).
        * Otherwise stack vertically (2x1).
    """
    logging.info(f"Generating Matplotlib combined diagnostics plot for {isotope}, Analysis {analysis_id}")
    figure.clear()

    memory_fits, drift_fits, data = fit_data

    # Decide layout based on figure size (tune thresholds as needed for your PyQt canvas)
    fig_w, fig_h = figure.get_figwidth(), figure.get_figheight()
    side_by_side = (fig_w >= 9.0) and (fig_h >= 4.0)

    if side_by_side:
        gs = GridSpec(nrows=1, ncols=2, figure=figure, left=0.07, right=0.98, top=0.93, bottom=0.10, wspace=0.25)
        ax1 = figure.add_subplot(gs[0, 0])  # drift
        ax2 = figure.add_subplot(gs[0, 1])  # memory
    else:
        gs = GridSpec(nrows=2, ncols=1, figure=figure, left=0.08, right=0.98, top=0.93, bottom=0.08, hspace=0.35)
        ax1 = figure.add_subplot(gs[0, 0])  # drift
        ax2 = figure.add_subplot(gs[1, 0])  # memory

    # --- Drift (identical look as the standalone) ---
    _draw_drift_ax(ax1, drift_fits, data, isotope)

    # --- Memory ---
    def _pick_mem(mem_dict, aid):
        """Return (fit_dict, used_key). Supports int/str keys and optional 'analyses' nesting."""
        if not isinstance(mem_dict, dict) or not mem_dict:
            return None, None
        sid = str(aid)
        # flat first
        for k in (aid, sid, 0, "0"):
            if k in mem_dict:
                return mem_dict[k], k
        # nested
        sub = mem_dict.get("analyses")
        if isinstance(sub, dict) and sub:
            for k in (aid, sid, 0, "0"):
                if k in sub:
                    return sub[k], k
            k = next(iter(sub.keys()))
            return sub[k], k
        # first available
        k = next(iter(mem_dict.keys()), None)
        return (mem_dict[k], k) if k is not None else (None, None)

    iso_bucket = (memory_fits or {}).get(isotope, {}) or {}
    fit_data_mem, used_key = _pick_mem(iso_bucket, analysis_id)

    mem_title = f'Standard-Characterized Memory Fit for {isotope}' if str(used_key) == "0" \
                else f'Memory Fit for {isotope} - Analysis {used_key}'

    if not fit_data_mem:
        ax2.text(0.5, 0.5, "No memory fit data available.", ha='center', va='center')
        ax2.set_title(mem_title); ax2.set_xlabel('Injection Number')
        ax2.set_ylabel("Memory Fraction" if str(used_key) == "0" else isotope)
        ax2.grid(True)
    else:
        # safe getters that don't 'or' on arrays
        def g(*names):
            for n in names:
                v = fit_data_mem.get(n)
                if v is not None: return v
            return None

        x_raw, y_raw = g("x_raw"), g("y_raw")
        x_fit        = g("x_fit", "x")
        y_tot        = g("y_fit_total", "y_fit", "pred")
        y_fast, y_slow = g("y_fit_fast"), g("y_fit_slow")

        if x_fit is None and x_raw is not None:
            xr = np.asarray(x_raw, float)
            if xr.size:
                x_fit = np.linspace(np.nanmin(xr), np.nanmax(xr), 200)

        # raw
        if x_raw is not None and y_raw is not None:
            xr, yr = np.asarray(x_raw, float), np.asarray(y_raw, float)
            if xr.size and yr.size:
                ax2.plot(xr, yr, 'o', label='Raw / Fractions')

        # fits
        plotted = False
        if x_fit is not None:
            xf = np.asarray(x_fit, float)
            if y_fast is not None:
                ax2.plot(xf, np.asarray(y_fast, float), '--', label='Fast'); plotted = True
            if y_slow is not None:
                ax2.plot(xf, np.asarray(y_slow, float), '--', label='Slow'); plotted = True
            if y_tot is not None:
                ax2.plot(xf, np.asarray(y_tot,  float), '-', color='red', lw=2, label='Fit'); plotted = True

        # labels
        ax2.set_title(mem_title)
        ax2.set_xlabel('Injection Number')
        ax2.set_ylabel("Memory Fraction" if str(used_key) == "0" else isotope)
        if plotted or (x_raw is not None and y_raw is not None):
            ax2.legend()
        ax2.grid(True)

        # neat integer ticks for small ranges
        try:
            xs = []
            if x_raw is not None:
                arr = np.asarray(x_raw, float); 
                if arr.size: xs += [arr.min(), arr.max()]
            if x_fit is not None:
                arr = np.asarray(x_fit, float)
                if arr.size: xs += [arr.min(), arr.max()]
            if xs and np.isfinite(xs).all() and (max(xs) - min(xs)) <= 50:
                ax2.set_xticks(np.arange(np.floor(min(xs)), np.ceil(max(xs)) + 1, 1))
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    figure.set_tight_layout(True)


def _normkey(s: str) -> str:
    """Lowercase and remove non-alphanumerics, so 'Z-Score' -> 'zscore'."""
    return re.sub(r'[^a-z0-9]+', '', str(s or '').lower())

# --- FIX: Replaced this function with the user's inline version ---
def _normalize_validation_rows(validation_results, present_isos: set | None = None) -> list[dict]:
    """
    Normalize validation payload into a list of rows:
      [{'Isotope': 'd18O', 'Z-Score': float|None, 'Uncertainty': float|None, 'Status': 'Pass/Warn/Fail/...'}, ...]
    Accepts:
      - dict: { 'd18O': {'d18O_z' or 'z_score' or 'Z-Score' or 'z', 'status', 'd18O_u'/'uncertainty'/...}, ... }
      - DataFrame: wide (d18O_z, dD_z, … and d18O_u, …) or long (isotope + z/z_score/Z-Score [+ uncertainty] [+ status])
    """
    rows: list[dict] = []

    # --- Case A: dict-like (preferred for per-isotope payloads)
    if isinstance(validation_results, dict):
        for iso, rec in validation_results.items():
            if not iso or (present_isos and iso not in present_isos):
                continue
            if rec is None:
                continue

            z = None; unc = None; status = ""
            if isinstance(rec, dict):
                for k in (f"{iso}_z", "Z-Score", "z_score", "zscore", "z"):
                    if k in rec: z = rec[k]; break
                for uk in (f"{iso}_u", "uncertainty", "u", "sd", "sem"):
                    if uk in rec: unc = rec[uk]; break
                for sk in (f"{iso}_status","status","flag","result"):
                    if sk in rec: status = str(rec[sk]); break
            
            rows.append({
                "Isotope": str(iso),
                "Z-Score": pd.to_numeric(z, errors="coerce"),
                "Uncertainty": pd.to_numeric(unc, errors="coerce"),
                "Status": status
            })
        return rows

    # --- Case B: DataFrame
    if isinstance(validation_results, pd.DataFrame) and not validation_results.empty:
        df = validation_results.copy()
        
        # Long form check: has 'isotope' + ('z'/'z_score'/'Z-Score') columns
        cols_norm = {_normkey(c): c for c in df.columns}
        iso_col = cols_norm.get("isotope") or cols_norm.get("iso")
        z_col = None
        for key in ("zscore","z_score","z-score","z"):
            if key in cols_norm: z_col = cols_norm[key]; break
        unc_col = cols_norm.get("uncertainty") or cols_norm.get("u") or cols_norm.get("sd") or cols_norm.get("sem")
        statcol = cols_norm.get("status") or cols_norm.get("flag") or cols_norm.get("result")

        if iso_col and z_col:
            for _, r in df.iterrows():
                iso = str(r.get(iso_col, ""))
                if not iso or (present_isos and iso not in present_isos):
                    continue
                rows.append({
                    "Isotope": iso,
                    "Z-Score": pd.to_numeric(r.get(z_col), errors="coerce"),
                    "Uncertainty": pd.to_numeric(r.get(unc_col), errors="coerce") if unc_col else np.nan,
                    "Status": str(r.get(statcol, "")) if statcol else ""
                })
            return rows

        # Wide form check: *_z columns (and possibly *_u)
        # This form (one row per sample) needs to be summarized for the report.
        z_cols = [c for c in df.columns if isinstance(c, str) and c.endswith("_z")]
        if z_cols:
            summary_rows = []
            for zc in z_cols:
                iso = zc[:-2]  # strip "_z"
                if not iso or (present_isos and iso not in present_isos):
                    continue
                
                z_series = pd.to_numeric(df[zc], errors='coerce').dropna()
                if z_series.empty:
                    continue # No z-scores for this isotope
                    
                max_abs_z = z_series.abs().max()
                n_fail = (z_series.abs() > 3.0).sum() # Example: count > 3
                n_warn = (z_series.abs() > 2.0).sum() - n_fail # Count > 2 but <= 3
                
                status = "PASS"
                if n_fail > 0: status = "FAIL"
                elif n_warn > 0: status = "WARN"
                
                # Find uncertainty if present
                ucol = f"{iso}_u" if f"{iso}_u" in df.columns else None
                unc = pd.to_numeric(df[ucol], errors="coerce").mean() if ucol else np.nan

                summary_rows.append({
                    "Isotope": iso,
                    "Z-Score": max_abs_z, # Show the max Z
                    "Uncertainty": unc,
                    "Status": status
                })
            return summary_rows

    return rows  # fallback

def plot_cn_scatter(
    data: pd.DataFrame,
    x: str = "d13C",
    y: str = "d15N",
    title: str = "δ13C vs δ15N",
    output_file: str | None = None,
    mwl=None,  # accepted for GUI compatibility (unused here)
    **kwargs,
):
    if data is None or getattr(data, "empty", True):
        raise ValueError("No data to plot.")
    for col in (x, y):
        if col not in data.columns:
            raise KeyError(f"Missing column for CN scatter: {col}")

    fig = go.Figure()
    fig.add_scatter(
        x=data[x], y=data[y], mode="markers", name="EA samples",
        text=data.get("sample_id"), hovertemplate="%{text}<br>%{x}, %{y}<extra></extra>"
    )
    fig.update_layout(
        title=title,
        xaxis_title=f"{x} (‰)",
        yaxis_title=f"{y} (‰)",
        template="plotly_white",
    )

    if output_file:
        try:
            fig.write_html(output_file, include_plotlyjs="cdn")
        except Exception:
            fig.write_html(output_file, include_plotlyjs=True, full_html=True)
    return fig


# --- δ13C vs δ15N (Matplotlib / canvas) ---
def plot_cn_scatter_mpl(
    fig,  # Matplotlib Figure injected by the GUI
    data: pd.DataFrame,
    x: str = "d13C",
    y: str = "d15N",
    title: str = "δ13C vs δ15N",
    mwl=None,  # accepted for GUI compatibility (unused here)
    **kwargs,
):
    if data is None or getattr(data, "empty", True):
        raise ValueError("No data to plot.")
    for col in (x, y):
        if col not in data.columns:
            raise KeyError(f"Missing column for CN scatter: {col}")

    # Clear and draw on the provided figure
    try:
        fig.clf()
    except Exception as e:

        logging.warning(f"Exception caught: {e}")
    ax = fig.add_subplot(111)
    ax.scatter(data[x], data[y], s=18)
    ax.set_title(title)
    ax.set_xlabel(f"{x} (‰)")
    ax.set_ylabel(f"{y} (‰)")
    fig.tight_layout()
    return fig


# === IRMS calibration plots ===
def plot_irms_calibration(data: pd.DataFrame, isotope: str = 'd18O', title: str | None = None,
                          output_file: str | None = None, mwl=None, fits: dict | None = None, **kwargs):
    import plotly.graph_objects as go
    iso = isotope
    meas = iso
    true = f"{iso}_true"
    if true not in data.columns or meas not in data.columns:
        raise KeyError(f"Calibration plot requires columns '{meas}' and '{true}'.")
    df = data[data.get('is_standard', False).astype(bool) & data[true].notna() & data[meas].notna()]
    if df.empty:
        raise ValueError("No standard rows with true values to plot.")
    fig = go.Figure()
    fig.add_scatter(x=df[meas], y=df[true], mode='markers', name='Standards')
    lo = float(min(df[meas].min(), df[true].min()))
    hi = float(max(df[meas].max(), df[true].max()))
    fig.add_scatter(x=[lo, hi], y=[lo, hi], mode='lines', name='1:1', line=dict(dash='dash'))
    if fits and iso in fits:
        a = fits[iso].get('a', 0.0); b = fits[iso].get('b', 1.0); r2 = fits[iso].get('r2', None)
        xs = pd.Series([lo, hi]); ys = a + b * xs
        fig.add_scatter(x=xs, y=ys, mode='lines', name=f"Fit (a={a:.3f}, b={b:.3f}, R²={r2:.3f})")
    fig.update_layout(title=title or f"Calibration: {iso} (measured vs true)",
                      xaxis_title=f"{iso} measured (‰)", yaxis_title=f"{iso} true (‰)")
    if output_file:
        try: fig.write_html(output_file, include_plotlyjs='cdn')
        except Exception: fig.write_html(output_file, include_plotlyjs=True, full_html=True)
    return fig

def plot_irms_calibration_mpl(fig: "Figure", data: pd.DataFrame, isotope: str = 'd18O',
                              title: str | None = None, mwl=None, fits: dict | None = None, **kwargs):
    iso = isotope
    meas = iso
    true = f"{iso}_true"
    if true not in data.columns or meas not in data.columns:
        raise KeyError(f"Calibration plot requires columns '{meas}' and '{true}'.")
    df = data[data.get('is_standard', False).astype(bool) & data[true].notna() & data[meas].notna()]
    if df.empty:
        raise ValueError("No standard rows with true values to plot.")
    ax = fig.add_subplot(111)
    ax.scatter(df[meas], df[true], s=18, label='Standards')
    lo = float(min(df[meas].min(), df[true].min()))
    hi = float(max(df[meas].max(), df[true].max()))
    ax.plot([lo, hi], [lo, hi], linestyle='--', label='1:1')
    if fits and iso in fits:
        a = fits[iso].get('a', 0.0); b = fits[iso].get('b', 1.0); r2 = fits[iso].get('r2', None)
        xs = np.array([lo, hi], dtype=float); ys = a + b * xs
        ax.plot(xs, ys, label=f"Fit (a={a:.3f}, b={b:.3f}, R²={r2:.3f})")
    ax.set_xlabel(f"{iso} measured (‰)"); ax.set_ylabel(f"{iso} true (‰)")
    ax.set_title(title or f"Calibration: {iso} (measured vs true)")
    ax.legend(); fig.tight_layout()
    return fig

# === IRMS diagnostics plots (EA/DI) ===========================================

def _std_mask_local(df: pd.DataFrame) -> pd.Series:
    keys = {"IHSTD","SCTRL","CTRL","CONTROL","CAL","CALIB","CALSTD","STD","QC","QCTRL"}
    if "role_code" not in df.columns:
        return pd.Series(False, index=df.index)
    return df["role_code"].astype(str).str.upper().isin(keys)

# --- EA LINEARITY DIAGNOSTIC (MPL) ---
def plot_ea_linearity_diag_mpl(fig, data, iso: str, fit: dict, amount_col="Amount", **kwargs):
    import matplotlib.pyplot as plt
    if fig is None: fig = Figure(figsize=(6,4))
    ax = fig.add_subplot(111)
    if data is None or data.empty or not fit:
        ax.text(0.5,0.5,"No data/fit", ha="center"); fig.tight_layout(); return fig

    x = pd.to_numeric(data[amount_col], errors="coerce")
    y = pd.to_numeric(data[iso], errors="coerce")
    m = x.notna() & y.notna()
    ax.scatter(x[m], y[m], s=14, alpha=0.8, label="All")

    xs = np.linspace(np.nanmin(x[m]), np.nanmax(x[m]), 100)
    if fit["model"] == "quadratic":
        a2, a1, a0 = fit["coeffs"]["a2"], fit["coeffs"]["a1"], fit["coeffs"]["a0"]
        ys = a2*xs**2 + a1*xs + a0
    else:
        a,b = fit["coeffs"]["a"], fit["coeffs"]["b"]
        ys = a*xs + b
    ax.plot(xs, ys, lw=2, label="Fit")

    sd = fit.get("sd_resid", np.nan); r2 = fit.get("r2", np.nan); n = fit.get("n", 0)
    ax.set_xlabel(f"{amount_col}")
    ax.set_ylabel(f"{iso} (‰)")
    ax.set_title(f"EA Linearity — {iso}")
    ax.legend(loc="best")
    ax.text(0.02, 0.98, f"n={n}, R²={r2:.3f}, sd_resid={sd:.3f}‰", transform=ax.transAxes,
            va="top", ha="left", fontsize=9)
    fig.tight_layout()
    return fig

# --- EA LINEARITY DIAGNOSTIC (Plotly) ---
def plot_ea_linearity_diag(data, iso: str, fit: dict, amount_col="Amount", **kwargs):
    import plotly.graph_objects as go
    fig = go.Figure()
    if data is None or data.empty or not fit:
        fig.update_layout(title="No data/fit"); return fig
    x = pd.to_numeric(data[amount_col], errors="coerce")
    y = pd.to_numeric(data[iso], errors="coerce")
    m = x.notna() & y.notna()
    fig.add_scatter(x=x[m], y=y[m], mode="markers", name="All")
    xs = np.linspace(np.nanmin(x[m]), np.nanmax(x[m]), 100)
    if fit["model"] == "quadratic":
        a2, a1, a0 = fit["coeffs"]["a2"], fit["coeffs"]["a1"], fit["coeffs"]["a0"]
        ys = a2*xs**2 + a1*xs + a0
    else:
        a,b = fit["coeffs"]["a"], fit["coeffs"]["b"]
        ys = a*xs + b
    fig.add_scatter(x=xs, y=ys, mode="lines", name="Fit")
    sd = fit.get("sd_resid", np.nan); r2 = fit.get("r2", np.nan); n = fit.get("n", 0)
    fig.update_layout(title=f"EA Linearity — {iso} (n={n}, R²={r2:.3f}, sd={sd:.3f}‰)",
                      xaxis_title=amount_col, yaxis_title=f"{iso} (‰)")
    return fig

# --- EA DRIFT DIAGNOSTIC (MPL) ---
def plot_ea_drift_diag_mpl(fig, data, iso: str, fit: dict, axis="order", **kwargs):
    import matplotlib.pyplot as plt
    if fig is None: fig = Figure(figsize=(6,4))
    ax = fig.add_subplot(111)
    if data is None or data.empty or not fit:
        ax.text(0.5,0.5,"No data/fit", ha="center"); fig.tight_layout(); return fig

    if (axis or fit.get("axis","order")).startswith("time"):
        if "timestamp" not in data.columns:
            ax.text(0.5,0.5,"No timestamp", ha="center"); return fig
        x = pd.to_datetime(data["timestamp"], errors="coerce").astype("int64")/1e9
        xlabel = "time (s)"
    else:
        x = pd.Series(np.arange(1, len(data)+1), index=data.index, dtype=float)
        xlabel = "order"

    y = pd.to_numeric(data[iso], errors="coerce")
    m = x.notna() & y.notna()
    ax.scatter(x[m], y[m], s=14, alpha=0.8, label="All")

    a = fit["coeffs"]["a"]; b = fit["coeffs"]["b"]
    xs = np.linspace(np.nanmin(x[m]), np.nanmax(x[m]), 100)
    ys = a*xs + b
    ax.plot(xs, ys, lw=2, label="Fit")

    sd = fit.get("sd_resid", np.nan); r2 = fit.get("r2", np.nan); n = fit.get("n", 0)
    ax.set_xlabel(xlabel); ax.set_ylabel(f"{iso} (‰)")
    ax.set_title(f"EA Drift — {iso} ({fit.get('axis','order')})")
    ax.legend(loc="best")
    ax.text(0.02, 0.98, f"n={n}, R²={r2:.3f}, sd_resid={sd:.3f}‰", transform=ax.transAxes,
            va="top", ha="left", fontsize=9)
    fig.tight_layout()
    return fig

# --- EA DRIFT DIAGNOSTIC (Plotly) ---
def plot_ea_drift_diag(data, iso: str, fit: dict, axis="order", **kwargs):
    import plotly.graph_objects as go
    if data is None or data.empty or not fit:
        fig = go.Figure(); fig.update_layout(title="No data/fit"); return fig
    if (axis or fit.get("axis","order")).startswith("time"):
        if "timestamp" not in data.columns:
            fig = go.Figure(); fig.update_layout(title="No timestamp"); return fig
        x = pd.to_datetime(data["timestamp"], errors="coerce").astype("int64")/1e9
        xlabel = "time (s)"
    else:
        x = pd.Series(np.arange(1, len(data)+1), index=data.index, dtype=float)
        xlabel = "order"
    y = pd.to_numeric(data[iso], errors="coerce")
    m = x.notna() & y.notna()
    fig = go.Figure()
    fig.add_scatter(x=x[m], y=y[m], mode="markers", name="All")
    a = fit["coeffs"]["a"]; b = fit["coeffs"]["b"]
    xs = np.linspace(np.nanmin(x[m]), np.nanmax(x[m]), 100)
    ys = a*xs + b
    fig.add_scatter(x=xs, y=ys, mode="lines", name="Fit")
    sd = fit.get("sd_resid", np.nan); r2 = fit.get("r2", np.nan); n = fit.get("n", 0)
    fig.update_layout(title=f"EA Drift — {iso} ({fit.get('axis','order')})  n={n}, R²={r2:.3f}, sd={sd:.3f}‰",
                      xaxis_title=xlabel, yaxis_title=f"{iso} (‰)")
    return fig

# ---------- Linearity vs Amount/Area (standards) ----------
def plot_linearity_standards_mpl(fig, data: pd.DataFrame, isotope: str, y_col: str = None, amount_col: str = None, title: str = None):
    
    ax = fig.add_subplot(111)
    if data is None or data.empty:
        ax.text(0.5,0.5,"No data", ha="center", va="center"); fig.tight_layout(); return fig
    amt = amount_col or next((c for c in ["Amount","Area","Peak Area","PeakArea"] if c in data.columns), None)
    ycol = y_col or (f"{isotope}_calibrated" if f"{isotope}_calibrated" in data.columns else isotope)
    if amt is None or ycol not in data.columns:
        ax.text(0.5,0.5,"Required columns missing", ha="center", va="center"); fig.tight_layout(); return fig
    d = data.loc[_std_mask_local(data), [amt, ycol]].dropna()
    if d.empty:
        ax.text(0.5,0.5,"No standards", ha="center", va="center"); fig.tight_layout(); return fig
    x = d[amt].to_numpy(float); y = d[ycol].to_numpy(float)
    ax.scatter(x, y, s=18)
    if x.size >= 2:
        p = np.polyfit(x, y, 1); xs = np.linspace(np.nanmin(x), np.nanmax(x), 100); ys = p[0]*xs + p[1]
        ax.plot(xs, ys)
    ax.set_xlabel(amt); ax.set_ylabel(f"{ycol} (‰)")
    ax.set_title(title or f"Linearity (standards) — {ycol} vs {amt}")
    fig.tight_layout()
    return fig

def plot_linearity_standards(data: pd.DataFrame, isotope: str, y_col: str = None, amount_col: str = None, title: str = None, output_file: str = None):

    if data is None or data.empty:
        fig = go.Figure(); fig.update_layout(title="No data")
    else:
        amt = amount_col or next((c for c in ["Amount","Area","Peak Area","PeakArea"] if c in data.columns), None)
        ycol = y_col or (f"{isotope}_calibrated" if f"{isotope}_calibrated" in data.columns else isotope)
        d = data.loc[_std_mask_local(data), [amt, ycol]].dropna() if (amt and ycol in data.columns) else pd.DataFrame()
        fig = go.Figure()
        if not d.empty:
            fig.add_scatter(x=d[amt], y=d[ycol], mode="markers", name="Standards")
            if len(d) >= 2:
                p = np.polyfit(d[amt].to_numpy(float), d[ycol].to_numpy(float), 1)
                xs = np.linspace(d[amt].min(), d[amt].max(), 100); ys = p[0]*xs + p[1]
                fig.add_scatter(x=xs, y=ys, mode="lines", name="Fit")
            fig.update_layout(xaxis_title=amt, yaxis_title=f"{ycol} (‰)")
        fig.update_layout(title=title or f"Linearity (standards) — {ycol} vs {amt}")
    if output_file:
        fig.write_html(output_file, include_plotlyjs="cdn")
    return fig

# ---------- Drift vs injection/time (standards) ----------
def plot_drift_standards_mpl(fig, data: pd.DataFrame, isotope: str, y_col: str = None, x_col: str = None, title: str = None):
    ax = fig.add_subplot(111)
    if data is None or data.empty:
        ax.text(0.5,0.5,"No data", ha="center", va="center"); fig.tight_layout(); return fig
    if x_col is None:
        x_col = "injection_no" if "injection_no" in data.columns else ("timestamp" if "timestamp" in data.columns else None)
    ycol = y_col or (f"{isotope}_calibrated" if f"{isotope}_calibrated" in data.columns else isotope)
    if x_col is None or ycol not in data.columns or x_col not in data.columns:
        ax.text(0.5,0.5,"Required columns missing", ha="center", va="center"); fig.tight_layout(); return fig
    d = data.loc[_std_mask_local(data), [x_col, ycol]].dropna()
    if d.empty:
        ax.text(0.5,0.5,"No standards", ha="center", va="center"); fig.tight_layout(); return fig
    is_datetime = (x_col == "timestamp")
    if is_datetime:
        x = pd.to_datetime(d[x_col], errors="coerce")
        x_num = x.map(lambda t: t.toordinal())
    else:
        x = d[x_col]
        x_num = x
    y = d[ycol]
    ax.scatter(x, y, s=18)
    if len(d) >= 2:
        p = np.polyfit(x_num.to_numpy(float), y.to_numpy(float), 1)
        if is_datetime:
            xs_num = np.linspace(np.nanmin(x_num), np.nanmax(x_num), 100)
            xs = [pd.Timestamp.fromordinal(int(v)) for v in xs_num]
        else:
            xs = np.linspace(np.nanmin(x_num), np.nanmax(x_num), 100)
            xs_num = xs
        ys = p[0] * np.array(xs_num, float) + p[1]
        ax.plot(xs, ys)
    ax.set_xlabel('Time' if is_datetime else x_col)
    ax.set_ylabel(f"{ycol} (‰)")
    ax.set_title(title or f"Drift (standards) — {ycol} vs {x_col}")
    if is_datetime:
        _apply_datetime_xaxis(ax)
    fig.set_tight_layout(True)
    return fig

def plot_drift_standards(data: pd.DataFrame, isotope: str, y_col: str = None, x_col: str = None, title: str = None, output_file: str = None):
    import plotly.graph_objects as go
    if data is None or data.empty:
        fig = go.Figure(); fig.update_layout(title="No data")
    else:
        x_col = x_col or ("injection_no" if "injection_no" in data.columns else ("timestamp" if "timestamp" in data.columns else None))
        ycol = y_col or (f"{isotope}_calibrated" if f"{isotope}_calibrated" in data.columns else isotope)
        d = data.loc[_std_mask_local(data), [x_col, ycol]].dropna() if (x_col and ycol in data.columns and x_col in data.columns) else pd.DataFrame()
        fig = go.Figure()
        if not d.empty:
            xx = pd.to_datetime(d[x_col], errors="coerce").map(lambda t: t.toordinal()) if x_col == "timestamp" else d[x_col]
            fig.add_scatter(x=xx, y=d[ycol], mode="markers", name="Standards")
            if len(d) >= 2:
                p = np.polyfit(np.asarray(xx, dtype=float), d[ycol].to_numpy(float), 1)
                xs = np.linspace(np.nanmin(np.asarray(xx, dtype=float)), np.nanmax(np.asarray(xx, dtype=float)), 100)
                ys = p[0]*xs + p[1]
                fig.add_scatter(x=xs, y=ys, mode="lines", name="Fit")
            fig.update_layout(xaxis_title=x_col, yaxis_title=f"{ycol} (‰)")
        fig.update_layout(title=title or f"Drift (standards) — {ycol} vs {x_col}")
    if output_file:
        fig.write_html(output_file, include_plotlyjs="cdn")
    return fig

# --- Robust reporting helpers (paste/replace in plots.py) ---

_ISOS = ("d18O","dD","d17O","d13C","d15N")

def _present_isotopes(df: pd.DataFrame) -> list[str]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    cols = {str(c) for c in df.columns}
    out = []
    for iso in _ISOS:
        if any((iso == c) or c.startswith(iso + "_") for c in cols):
            out.append(iso)
    return out

def _filter_unknown_only(df: pd.DataFrame) -> pd.DataFrame:
    """Return only unknown/sample rows (strict when 'role' exists)."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    cols = set(map(str, df.columns))
    if "role" in cols:
        # STRICT: only keep rows explicitly labeled as Sample
        mask = df["role"].astype(str).str.strip().str.lower().eq("sample")
        return df[mask].copy()
    # Fallback (very permissive): try to exclude common standards by role_code
    if "role_code" in cols:
        standards_like = {"AMLIN","IHSTD","SCTRL","CAL","MEM","DIWSH"}
        mask = ~df["role_code"].astype(str).str.upper().isin(standards_like)
        return df[mask].copy()
    return df.copy()

def _unknown_rows_and_headers(df: pd.DataFrame, present_isos: list):
    """
    Build (rows, headers) for the Unknown Samples table.
    - Base columns: sample_id, Analysis, etc. (only those present)
    - For each iso: prefer *_calibrated (then *_final, *_corr, then raw), and an uncertainty column if present.
    """
    rows, headers = [], []
    if not isinstance(df, pd.DataFrame) or df.empty:
        return rows, headers

    # Base identifiers if present (order preserved)
    base_candidates = ['sample_id','sample_name', 'Sample ID','SAMPLE_ID','Analysis'] #,'analysis','analysis_id'] #,'Block','block_no']
    base_cols = [c for c in base_candidates if c in df.columns]

    # Per-isotope columns
    iso_cols = []
    for iso in present_isos:
        main_candidates = [f"{iso}_calibrated", f"{iso}_final", f"{iso}_corr"] #, iso]
        u_candidates    = [f"{iso}_u", f"{iso}_calibrated_u", f"{iso}_sd", f"{iso}_se"]
        main = next((c for c in main_candidates if c in df.columns), None)
        if main:
            iso_cols.append(main)
            ucol = next((c for c in u_candidates if c in df.columns), None)
            if ucol:
                iso_cols.append(ucol)

    headers = base_cols + iso_cols
    if not headers:
        return rows, headers

    # Assemble, with light numeric formatting
    sub = df[headers].copy()
    for _, r in sub.iterrows():
        d = {}
        for h, v in r.items():
            if isinstance(v, float):
                d[h] = f"{v:.3f}"
            else:
                d[h] = v
        rows.append(d)
    return rows, headers

def generate_reports(analysis_data: pd.DataFrame,
                     injection_data: pd.DataFrame,
                     validation_results,
                     output_dir: str,
                     mwl: str = None,
                     memory_fits: dict | None = None,
                     drift_fits: dict | None = None,
                     standards_df: pd.DataFrame | None = None) -> None:
    """
    - Write processed CSVs (if present)
    - Write validation_report.csv (preserves Z-Score/Status if available)
    - Generate optional Drift & Memory plots (PNG) if fits provided
    - HTML shows Unknown table + diagnostics with sample_ids used per Standards selection
    """
    import matplotlib
    matplotlib.use("Agg")  # headless
    import matplotlib.pyplot as plt

    os.makedirs(output_dir, exist_ok=True)

    if isinstance(analysis_data, pd.DataFrame) and not analysis_data.empty:
        analysis_data.to_csv(os.path.join(output_dir, "processed_analysis_data.csv"), index=False)
    if isinstance(injection_data, pd.DataFrame) and not injection_data.empty:
        injection_data.to_csv(os.path.join(output_dir, "processed_injection_data.csv"), index=False)

    present_isos = set(_present_isotopes(analysis_data if isinstance(analysis_data, pd.DataFrame) else pd.DataFrame()))
    val_rows = []
    try:
        val_rows = _normalize_validation_rows(validation_results, present_isos)

    except Exception as e_val:
        logging.warning(f"Could not normalize validation results for report: {e_val}")
        val_rows = []
    # Map to the exact keys your HTML expects and format
    val_rows_html = []
    for r in (val_rows or []):
        rr = dict(r)

        # unify Z key -> "Z"
        if "Z" not in rr:
            for k in ("Z-Score", "z_score", "z", "ZScore", "zscore"):
                if k in rr and rr[k] is not None:
                    rr["Z"] = rr.pop(k)
                    break

        iso = rr.get("Isotope") or rr.get("isotope") or rr.get("iso") or ""
        z_raw = rr.get("Z", "")
        status = rr.get("Status", "")

        # numeric Z formatting
        z_str = ""
        z_abs = None
        try:
            z_abs = abs(float(z_raw))
            z_str = f"{float(z_raw):.2f}"
        except Exception:
            z_str = str(z_raw) if z_raw not in (None, "") else ""

        # fill Status only if missing, based on |Z|
        if (not status) and (z_abs is not None):
            if z_abs <= 2.0:
                status = "PASS"
            elif z_abs <= 3.0:
                status = "WARN"
            else:
                status = "FAIL"

        val_rows_html.append({"Isotope": str(iso), "Z": z_str, "Status": status})

    # (optional) write CSV with the normalized keys
    if val_rows_html:
        pd.DataFrame(val_rows_html).to_csv(os.path.join(output_dir, "validation_report.csv"), index=False)    

    def _ids_from_standards(flag_col: str, iso: str) -> list[str]:
        try:
            sdf = standards_df
            if not isinstance(sdf, pd.DataFrame) or sdf.empty: return []
            sdf = sdf.copy()
            if "isotope" in sdf.columns:
                sf = sdf.loc[sdf["isotope"].astype(str) == str(iso)]
            else:
                sf = sdf
            flag_col_actual = None
            aliases = { "is_drift_id": ["is_drift_id","is_drift_std","is_drift","is_driftflag"],
                        "is_memory_id": ["is_memory_id","is_memory","is_memory_std","is_mem_std"] }
            for cand in aliases.get(flag_col, flag_col):
                if cand in sf.columns:
                    flag_col_actual = cand; break
            if flag_col_actual:
                mask = pd.to_numeric(sf[flag_col_actual], errors="coerce") == 1
                sid = sf.loc[mask, "sample_id"].astype(str).unique().tolist()
                return sorted(set(sid))
            return []
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return []

    def _sid_join(vals) -> str:
        try: return ", ".join(sorted({str(x) for x in (vals or []) if pd.notna(x)}))
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return ""

    drift_entries, memory_entries = [], []

    # -------- DRIFT PNGs (encoded) --------
    if isinstance(drift_fits, dict) and drift_fits:
        for iso, fit in drift_fits.items():
            if not fit or 'data' not in fit: continue
            # FIX: Use data from fit obj, fallback to analysis_data
            drift_plot_data = fit.get('data')
            if not _is_plottable(drift_plot_data):
                drift_plot_data = analysis_data if _is_plottable(analysis_data) else injection_data
                
            try:
                fig = plt.figure(figsize=(7.2, 4.4), dpi=120)
                # Pass (fits_dict, data_df) tuple to MPL func
                plot_drift_fit_mpl(fig, (drift_fits, drift_plot_data), iso, mwl=mwl)
                
                buf = io.BytesIO()
                fig.savefig(buf, format='png', bbox_inches="tight")
                plt.close(fig) # Close figure to save memory
                buf.seek(0)
                img_base64 = base64.b64encode(buf.read()).decode('utf-8')
                data_uri = f"data:image/png;base64,{img_base64}"
                
            except Exception as e:
                logging.warning(f"Failed to render drift plot for {iso}: {e}")
                plt.close(fig)
                continue

            used_ids = _ids_from_standards("is_drift_id", iso)
            drift_entries.append({
                "isotope": iso,
                "file_data_uri": data_uri, # Pass data URI
                "sample_ids": _sid_join(used_ids)
            })

    # -------- MEMORY PNGs (encoded) --------
    if isinstance(memory_fits, dict) and memory_fits:
        # from plots import plot_memory_fit_mpl # Already imported
        for iso, obj in memory_fits.items():
            if not isinstance(obj, dict) or not obj: continue
            keys = [k for k in obj.keys() if not (isinstance(k, str) and k.lower().startswith("sd"))]
            if not keys: continue
            aid = 0 if 0 in keys else keys[0]
            try:
                fig = plt.figure(figsize=(7.2, 4.4), dpi=120)
                plot_memory_fit_mpl(fig, memory_fits, iso, aid, mwl=mwl)
                
                # --- FIX: Save to buffer and encode ---
                buf = io.BytesIO()
                fig.savefig(buf, format='png', bbox_inches="tight")
                plt.close(fig)
                buf.seek(0)
                img_base64 = base64.b64encode(buf.read()).decode('utf-8')
                data_uri = f"data:image/png;base64,{img_base64}"

            except Exception as e:
                logging.warning(f"Failed to render memory plot for {iso} (aid={aid}): {e}")
                plt.close(fig)
                continue

            used_ids = _ids_from_standards("is_memory_id", iso)
            memory_entries.append({
                "isotope": iso,
                "file_data_uri": data_uri, # Pass data URI
                "sample_ids": _sid_join(used_ids)
            })

    generate_html_report(analysis_data,
                         val_rows_html,
                         output_dir,
                         drift_plots=drift_entries,
                         memory_plots=memory_entries)

def generate_html_report(analysis_data: pd.DataFrame,
                         validation_rows: list,
                         output_dir: str,
                         drift_plots: list | None = None,
                         memory_plots: list | None = None) -> None:
    if not isinstance(analysis_data, pd.DataFrame) or analysis_data.empty:
        unknown = pd.DataFrame()
    else:
        unknown = _filter_unknown_only(analysis_data)

    present = _present_isotopes(unknown)
    rows, headers = _unknown_rows_and_headers(unknown, present)
    vr_html = validation_rows or []

    template_str = """
<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Isotope Analysis Report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 font-sans">
  <div class="container mx-auto p-6">
    <h1 class="text-3xl font-bold mb-6 text-center">Isotope Analysis Report</h1>

    <h2 class="text-2xl font-semibold mb-3">Processed Results — Unknown Samples</h2>
    <div class="overflow-x-auto">
      <table class="min-w-full border-collapse bg-white shadow rounded">
        <thead>
          <tr class="bg-blue-600 text-white">
            {% for h in table_headers %}
            <th class="border p-3 text-left">{{ h }}</th>
            {% endfor %}
          </tr>
        </thead>
        <tbody>
          {% if rows %}
            {% for row in rows %}
            <tr class="hover:bg-gray-50">
              {% for h in table_headers %}
              <td class="border p-3">{{ row.get(h, "") }}</td>
              {% endfor %}
            </tr>
            {% endfor %}
          {% else %}
            <tr><td class="border p-3" colspan="{{ table_headers|length }}">No unknown samples to display.</td></tr>
          {% endif %}
        </tbody>
      </table>
    </div>

    <h2 class="text-2xl font-semibold mt-8 mb-3">Diagnostics — Drift & Memory</h2>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
      {% if drift_plots %}
        {% for d in drift_plots %}
        <div class="bg-white rounded shadow p-3">
          <div class="font-semibold mb-1">Drift: {{ d.isotope }}{% if d.sample_ids %} ({{ d.sample_ids }}){% endif %}</div>
          <!-- FIX: Use file_data_uri -->
          <img src="{{ d.file_data_uri }}" alt="Drift {{ d.isotope }}" class="w-full rounded border">
        </div>
        {% endfor %}
      {% endif %}

      {% if memory_plots %}
        {% for m in memory_plots %}
        <div class="bg-white rounded shadow p-3">
          <div class="font-semibold mb-1">Memory: {{ m.isotope }}{% if m.sample_ids %} ({{ m.sample_ids }}){% endif %}</div>
          <!-- FIX: Use file_data_uri -->
          <img src="{{ m.file_data_uri }}" alt="Memory {{ m.isotope }}" class="w-full rounded border">
        </div>
        {% endfor %}
      {% endif %}
    </div>

    <h2 class="text-2xl font-semibold mt-8 mb-3">Validation Results</h2>
    <div class="overflow-x-auto">
      <table class="min-w-full border-collapse bg-white shadow rounded">
        <thead>
          <tr class="bg-blue-600 text-white">
            <th class="border p-3 text-left">Isotope</th>
            <th class="border p-3 text-left">Z-Score</th>
            <th class="border p-3 text-left">Status</th>
          </tr>
        </thead>
        <tbody>
          {% if validation %}
            {% for v in validation %}
            <tr class="hover:bg-gray-50">
              <td class="border p-3">{{ v["Isotope"] }}</td>
              <td class="border p-3">{{ v["Z"] }}</td>
              <td class="border p-3 {% if v['Status'] and v['Status']|lower == 'fail' %}text-red-600{% elif v['Status'] and v['Status']|lower == 'warn' %}text-amber-600{% else %}text-green-700{% endif %}">
                {{ v["Status"] if v["Status"] else "—" }}
              </td>
            </tr>
            {% endfor %}
          {% else %}
            <tr><td class="border p-3" colspan="3">No validation rows.</td></tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>
</body></html>
"""
    from jinja2 import Template
    html = Template(template_str).render(
        rows=rows,
        table_headers=headers,
        validation=vr_html,
        drift_plots=drift_plots or [],
        memory_plots=memory_plots or []
    )
    out = os.path.join(output_dir, "isotope_report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)



