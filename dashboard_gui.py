"""
dashboard_gui.py — Main dashboard widget for the IsoWorks application.
Displays project-phase summary charts and a filterable sample table using
Matplotlib and SQLAlchemy, providing an at-a-glance laboratory overview.
"""
import sys
import logging
from datetime import datetime
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QRadioButton, QComboBox,
    QTableView, QLabel,
    QHeaderView, QSplitter, QToolButton, QTabWidget
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5.QtCore import Qt, QTimer, QPoint

# --- Matplotlib ---
import matplotlib
matplotlib.use('Qt5Agg')
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.pyplot as plt
import numpy as np

# --- NEW: Shared Manager & SQLAlchemy ---
from db_core import db_manager
from sqlalchemy import text
from shared_utils import IconCache  # noqa: F401 — kept for future icon usage
from help_browser import make_help_button

# --- Matplotlib Widget with Legend Toggle and Tooltips ---

# ── Phase display constants ──────────────────────────────────────────────────
# Order and colours match phaselookup.sort_order in the database.
# phase_label values must match phaselookup.label exactly.
_PHASE_ORDER  = ["Registered", "Pending", "Ongoing", "Analysed", "Evaluated", "Reported", "Cancelled"]
_PHASE_COLORS = {
    "Registered": "#5B9BD5",   # steel-blue
    "Pending":    "#ED7D31",   # amber
    "Ongoing":    "#70AD47",   # green
    "Analysed":   "#4BACC6",   # teal
    "Evaluated":  "#9B59B6",   # purple
    "Reported":   "#264478",   # dark navy
    "Cancelled":  "#BFBFBF",   # grey
}


class MatplotlibChartWidget(QWidget):
    """Canvas widget with three chart modes:
       • plot_doughnut()          – single donut (legacy / fallback)
       • plot_all_media()         – stacked horizontal bar by media (All Media view)
       • plot_single_media()      – two side-by-side donuts (phase + detail)
    """
    def __init__(self, parent=None):
        super().__init__(parent)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.figure  = Figure(facecolor='#F8F9FA')
        self.canvas  = FigureCanvas(self.figure)
        main_layout.addWidget(self.canvas)

        self.btn_legend = QToolButton(self.canvas)
        self.btn_legend.setText("☰ Legend")
        self.btn_legend.setCheckable(True)
        self.btn_legend.setChecked(False)
        self.btn_legend.setStyleSheet(
            "QToolButton{background:rgba(255,255,255,180);border:1px solid #ccc;"
            "border-radius:4px;padding:2px 6px;font-size:10px;}"
            "QToolButton:checked{background:rgba(200,230,255,200);}"
        )
        self.show_legend = False
        self.btn_legend.toggled.connect(self._toggle_legend)
        self.canvas.resizeEvent = self._on_canvas_resize

        self.current_chart_type = None  # Tracks active chart mode
        self.tooltip_label = None   # kept for legacy plot_doughnut
        self._tip_text     = None   # figure-level tooltip (plot_single_media / plot_all_media)
        self._wedge_data   = []     # list of (wedge, label, value, total) all donuts
        self._bar_data     = []     # list of (rect, phase, media, value, total) stacked bar
        self._legends      = []     # donut ax legends (plot_single_media)
        self.wedges        = []
        self.labels        = []
        self.data          = []
        self.legend        = None

        self.canvas.mpl_connect('motion_notify_event', self._on_mouse_move)
        self.canvas.mpl_connect('axes_leave_event',    self._on_mouse_leave)

    def _toggle_legend(self, checked):
        self.show_legend = checked
        # Bar chart legend
        if self.legend:
            self.legend.set_visible(checked)
        # Donut legends — also adjust bottom margin so they have room
        if self._legends:
            for leg in self._legends:
                leg.set_visible(checked)
            self._scale_elements()
        self.canvas.draw_idle()

    def _scale_elements(self, w=None, h=None):
        """Scale font sizes of all text elements dynamically based on canvas dimensions."""
        if w is None or h is None:
            w = self.canvas.width()
            h = self.canvas.height()
        if w <= 0 or h <= 0:
            return

        ref_w, ref_h = 750.0, 450.0
        scale_factor = min(w / ref_w, h / ref_h)
        # Clamp scale factor to a safe, readable range
        scale_factor = max(0.65, min(scale_factor, 1.5))

        from matplotlib.text import Text
        for text_obj in self.figure.findobj(match=Text):
            if not hasattr(text_obj, '_original_fontsize'):
                text_obj._original_fontsize = text_obj.get_size()
            new_size = text_obj._original_fontsize * scale_factor
            text_obj.set_size(new_size)

        # Force tight_layout recalculation for stacked bar charts
        if self.current_chart_type == "all_media":
            try:
                self.figure.tight_layout(pad=1.2)
            except Exception:
                pass
        elif self.current_chart_type == "single_media":
            try:
                # Calculate dynamic bottom margin if legend is checked
                if self.show_legend:
                    bottom_margin = (135.0 * scale_factor) / h
                    bottom_margin = max(0.12, min(bottom_margin, 0.35))
                else:
                    bottom_margin = 0.03
                self.figure.subplots_adjust(left=0.01, right=0.99, top=0.94, bottom=bottom_margin, wspace=0.05)
            except Exception:
                pass

    def _on_canvas_resize(self, event):
        if event is not None:
            w = event.size().width()
            h = event.size().height()
            self._scale_elements(w, h)
        FigureCanvas.resizeEvent(self.canvas, event)
        w = self.canvas.width()
        self.btn_legend.move(w - self.btn_legend.width() - 8, 6)

    def _on_mouse_move(self, event):
        hit = None

        # 1. Donut wedges
        for wedge, label, value, total in getattr(self, '_wedge_data', []):
            if wedge.contains_point([event.x, event.y]):
                hit = f"{label}: {value}  ({value/total*100:.1f}%)"
                break

        # 2. Stacked bar segments
        if hit is None:
            for rect, phase, media, value, total in getattr(self, '_bar_data', []):
                if rect.contains_point([event.x, event.y]):
                    hit = f"{media} — {phase}: {int(value)}  ({value/total*100:.1f}%)"
                    break

        if not hit and not getattr(self, '_wedge_data', []) and not getattr(self, '_bar_data', []):
            return

        # Draw tooltip in figure coords at bottom-centre
        if self._tip_text is None:
            self._tip_text = self.figure.text(
                0.5, 0.012, hit or "", ha='center', va='bottom', fontsize=8.5,
                bbox=dict(boxstyle='round,pad=0.3', fc='#FFFFCC', ec='#888', alpha=0.92)
            )
        else:
            self._tip_text.set_text(hit or "")
        self.canvas.draw_idle()

    def _on_mouse_leave(self, event):
        if self._tip_text:
            self._tip_text.set_text("")
            self.canvas.draw_idle()

    # ── public chart methods ────────────────────────────────────────────────

    def plot_all_media(self, rows):
        """
        Stacked horizontal bar chart: one bar per media, segments = aggregate phases.
        rows: list of (media_name, phase_label, status_desc, count)
        phase_label comes directly from phaselookup.label via vwSampleAnalysisStatus.
        """
        self.current_chart_type = "all_media"
        self.figure.clear()
        self._tip_text   = None
        self._bar_data   = []
        self._wedge_data = []
        self.wedges      = []

        if not rows:
            ax = self.figure.add_subplot(111)
            ax.text(0.5, 0.5, "No data", ha='center', va='center', fontsize=13, color='#666')
            ax.axis('off')
            self._scale_elements()
            self.canvas.draw()
            return

        # Aggregate: {media → {phase_label → count}}
        from collections import defaultdict
        media_phase = defaultdict(lambda: defaultdict(int))
        for media, phase_label, _desc, count in rows:
            media_phase[media][phase_label] += count

        media_names = sorted(media_phase.keys())
        phases      = [p for p in _PHASE_ORDER if any(media_phase[m].get(p, 0) > 0 for m in media_names)]
        totals      = [sum(media_phase[m].values()) for m in media_names]

        y  = np.arange(len(media_names))
        h  = max(0.35, min(0.65, 0.75 / max(len(media_names), 1)))

        ax = self.figure.add_subplot(111)
        ax.set_facecolor('#F8F9FA')
        self.figure.patch.set_facecolor('#F8F9FA')

        lefts = np.zeros(len(media_names))
        for phase in phases:
            vals = np.array([media_phase[m].get(phase, 0) for m in media_names], dtype=float)
            b = ax.barh(y, vals, left=lefts, height=h,
                        color=_PHASE_COLORS.get(phase, '#999'),
                        label=phase, edgecolor='white', linewidth=0.8)
            for i, (bar, val) in enumerate(zip(b, vals)):
                if val >= 1:
                    cx = bar.get_x() + bar.get_width() / 2
                    ax.text(cx, y[i], str(int(val)),
                            ha='center', va='center', fontsize=7.5, color='white', fontweight='bold')
                    self._bar_data.append((bar, phase, media_names[i], val, totals[i]))
            lefts += vals

        for i, (tot, left) in enumerate(zip(totals, lefts)):
            ax.text(left + max(lefts) * 0.01, y[i], f" {tot}",
                    va='center', fontsize=8, color='#333', fontweight='bold')

        ax.set_yticks(y)
        ax.set_yticklabels(media_names, fontsize=9)
        ax.invert_yaxis()
        ax.set_xlabel("Number of samples", fontsize=9)
        ax.set_title("Sample Status by Media", fontsize=11, fontweight='bold', pad=8)
        ax.tick_params(axis='x', labelsize=8)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.set_xlim(0, max(lefts) * 1.12)

        leg = ax.legend(loc='lower right', fontsize=8, frameon=True,
                        fancybox=True, framealpha=0.88, ncol=2)
        self.legend = leg
        leg.set_visible(self.show_legend)

        self.figure.tight_layout(pad=1.2)
        self._scale_elements()
        self.canvas.draw()
    def plot_single_media(self, rows, media_name):
        """
        Two side-by-side donuts for a single media:
          Left  — 4-phase aggregate  (numbered segments, % + count inside)
          Right — raw status detail  (same numbering scheme)
        Legend shows: "① Label" — no count (count lives inside the wedge).
        Tooltip is anchored at figure bottom — never overlaps legend or wedges.
        """
        self.current_chart_type = "single_media"
        self.figure.clear()
        self._tip_text   = None
        self._wedge_data = []
        self.wedges      = []

        if not rows:
            ax = self.figure.add_subplot(111)
            ax.text(0.5, 0.5, "No data", ha='center', va='center', fontsize=13, color='#666')
            ax.axis('off')
            self._scale_elements()
            self.canvas.draw()
            return

        from collections import defaultdict, OrderedDict

        phase_counts  = defaultdict(int)
        detail_counts = OrderedDict()
        for _media, phase_label, status_desc, count in sorted(rows, key=lambda r: r[1]):
            phase_counts[phase_label] += count
            detail_counts[status_desc] = detail_counts.get(status_desc, 0) + count

        agg_phases = [p for p in _PHASE_ORDER if phase_counts.get(p, 0) > 0]
        agg_vals   = [phase_counts[p] for p in agg_phases]
        agg_colors = [_PHASE_COLORS[p] for p in agg_phases]

        det_labels = list(detail_counts.keys())
        det_vals   = list(detail_counts.values())
        det_colors = plt.get_cmap('tab20')(np.linspace(0, 1, max(len(det_vals), 1)))

        # ── layout: leave room at top for suptitle, bottom for tooltip ────────
        # rect=[left, bottom, right, top]
        ax_agg, ax_det = self.figure.subplots(1, 2)
        ax_agg.set_aspect('equal')
        ax_det.set_aspect('equal')
        self.figure.patch.set_facecolor('#F8F9FA')

        def _draw_donut(ax, labels, vals, colors, title, center_txt):
            total = sum(vals)
            if total == 0:
                ax.text(0.5, 0.5, "No data", ha='center', va='center',
                        transform=ax.transAxes, fontsize=10, color='#888')
                ax.axis('off')
                return [], None

            wedges, _ = ax.pie(
                vals, startangle=90, colors=colors,
                wedgeprops=dict(width=0.50, edgecolor='white', linewidth=1.6)
            )

            # Centre label
            ax.text(0, 0, f"{center_txt}\n{total}" if center_txt else str(total),
                    ha='center', va='center', fontsize=11, fontweight='bold', color='#333')

            ax.set_title(title, fontsize=10, fontweight='bold', pad=6, color='#222')

            # Inside-wedge labels: % + count for large-enough segments
            for w, v in zip(wedges, vals):
                pct = v / total * 100
                if pct >= 4:
                    ang    = np.deg2rad((w.theta1 + w.theta2) / 2)
                    rx, ry = 0.72 * np.cos(ang), 0.72 * np.sin(ang)
                    ax.text(rx, ry, f"{pct:.0f}%\n({v})",
                            ha='center', va='center', fontsize=7,
                            color='white', fontweight='bold', linespacing=1.3)

            ncols = 2 if len(labels) > 5 else 1
            leg = ax.legend(
                wedges, labels,
                loc='upper center',
                bbox_to_anchor=(0.5, -0.04),
                ncol=ncols, fontsize=7.5,
                frameon=True, fancybox=True, framealpha=0.85, edgecolor='#ccc'
            )
            leg.set_visible(self.show_legend)
            return wedges, leg

        w_agg, leg_agg = _draw_donut(ax_agg, agg_phases, agg_vals, agg_colors, "Phase summary", "Total")
        w_det, leg_det = _draw_donut(ax_det, det_labels, det_vals, det_colors, "Status detail", "")

        self._legends = [l for l in (leg_agg, leg_det) if l is not None]

        # Register ALL wedges for hover tooltip (both donuts)
        tot_agg = sum(agg_vals)
        tot_det = sum(det_vals)
        self._wedge_data = (
            [(w, lbl, v, tot_agg) for w, lbl, v in zip(w_agg, agg_phases, agg_vals)] +
            [(w, lbl, v, tot_det) for w, lbl, v in zip(w_det, det_labels, det_vals)]
        )
        # Legacy compat
        self.wedges = w_agg
        self.labels = agg_phases
        self.data   = agg_vals
        self.figure.suptitle(f"Status overview — {media_name}",
                             fontsize=11, fontweight='bold', y=0.99)
        # Start compact (legends hidden); _toggle_legend expands to 0.30 when shown
        self.figure.subplots_adjust(left=0.01, right=0.99, top=0.94, bottom=0.30 if self.show_legend else 0.03, wspace=0.05)
        self._scale_elements()
        self.canvas.draw()

    def plot_doughnut(self, labels, data, title=""):
        """Legacy single-donut — kept for backward compatibility."""
        self.current_chart_type = "doughnut"
        self.figure.clear()
        self.tooltip_label = None
        self.labels = labels
        self.data   = data

        ax    = self.figure.add_subplot(111)
        ax.set_aspect('equal')
        total = sum(data)

        if total == 0:
            ax.text(0.5, 0.5, "No Data", ha='center', va='center', fontsize=14, color='#666')
            ax.axis('off')
            self._scale_elements()
            self.canvas.draw()
            return

        colors = plt.get_cmap('tab20c')(range(len(data)))
        self.wedges, _ = ax.pie(
            data, startangle=90, colors=colors,
            wedgeprops=dict(width=0.4, edgecolor='w', linewidth=2)
        )
        ax.set_xlim(-1.85, 1.85); ax.set_ylim(-1.75, 1.75)

        for wedge, label, value in zip(self.wedges, labels, data):
            pct = value / total * 100
            if pct < 2.5:
                continue
            ang = np.deg2rad((wedge.theta1 + wedge.theta2) / 2.0)
            cos_a, sin_a = np.cos(ang), np.sin(ang)
            x0, y0 = 1.06 * cos_a, 1.06 * sin_a
            x1, y1 = 1.30 * cos_a, 1.30 * sin_a
            h_offset = 0.30 if x1 >= 0 else -0.30
            x2, y2  = x1 + h_offset, y1
            ha      = 'left' if x1 >= 0 else 'right'
            ax.plot([x0, x1, x2], [y0, y1, y2], color='#888', lw=0.9, solid_capstyle='round')
            ax.text(x2 + (0.04 if ha == 'left' else -0.04), y2,
                    f"{label}\n{value}  ({pct:.1f}%)",
                    ha=ha, va='center', fontsize=7.5, color='#222', linespacing=1.4)

        ax.text(0, 0, f'Total\n{total}',
                ha='center', va='center', fontsize=13, weight='bold', color='#333')
        ax.set_title(title, fontsize=12, fontweight='bold', pad=10)
        self.legend = ax.legend(
            self.wedges, labels, title="Status", loc='upper right',
            bbox_to_anchor=(0.99, 0.99), bbox_transform=ax.transAxes,
            fontsize=7, frameon=True, fancybox=True, framealpha=0.88
        )
        self.legend.set_visible(self.show_legend)
        self.figure.subplots_adjust(left=0.01, right=0.99, top=0.93, bottom=0.01)
        self._scale_elements()
        self.canvas.draw()
class DashboardWidget(QWidget):
    """
    Port of frmHome, designed as a main dashboard widget.
    Refactored to use db_core (SQLAlchemy) and consistent table styling.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Models
        self.urgent_samples_model = QStandardItemModel()
        self.active_batches_model = QStandardItemModel()
        self.status_summary_model = QStandardItemModel()        
        # UI Layout
        self.main_layout = QVBoxLayout(self)        
        # Top: Filters
        self.main_layout.addWidget(self._create_filter_panel())        
        # Middle: Splitter (Left Lists | Right Charts)
        splitter = QSplitter(Qt.Horizontal)        
        # Left Panel
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self._create_urgent_samples_group())
        left_layout.addWidget(self._create_active_batches_group())        
        splitter.addWidget(left_panel)        
        # Right Panel: QTabWidget (Tab 1 = Chart, Tab 2 = Status Summary)
        right_tabs = QTabWidget()
        right_tabs.addTab(self._create_chart_widget(), "Status Overview")
        right_tabs.addTab(self._create_status_summary_group(), "Status Summary")
        splitter.addWidget(right_tabs)

        splitter.setSizes([500, 700])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        self.main_layout.addWidget(splitter, 1)        
        # Bottom: Status
        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #555; padding: 4px; font-size: 11px; border-top: 1px solid #ddd;")
        self.main_layout.addWidget(self.status_label)        
        # Signals
        self.cmbMedia.currentIndexChanged.connect(self.refresh_dashboard)
        self.optTBA.toggled.connect(self.refresh_dashboard)        
        # Init
        try:
            db_manager.get_engine()
            self.load_media_combo()
            QTimer.singleShot(100, self.refresh_dashboard)
        except Exception:
            self.status_label.setText("Database not connected.")

    # --- Standard Interface for Launcher ---
    def refresh(self):
        self.refresh_dashboard()

    # --- Styling Helper ---
    def _apply_table_styles(self, table_view):
        """Applies the clean, white/blue theme matching siam_run_list_gui."""

        table_view.setStyleSheet("""
        QTableView {
            border: 1px solid #d0d0d0;
            background-color: white;
            gridline-color: none;
        }
        QTableView::item {
            padding: 4px 6px;
            border-bottom: 1px solid #f0f0f0;
            color: #333333;
            text-align: left;
        }
        QTableView::item:alternate {
            background-color: #F7F9FC;
        }
        QTableView::item:selected {
            background-color: #DDEEFF;
            color: #000000;
        }
        QHeaderView::section {
            background-color: #f5f6f7;
            color: #5f6368;
            font-weight: bold;
            padding: 6px;
            border: none;
            border-bottom: 2px solid #d0d0d0;
            border-right: 1px solid #e0e0e0;
        }
        """)
        table_view.setShowGrid(False)
        table_view.setAlternatingRowColors(True)
        table_view.verticalHeader().setVisible(False)

    # --- UI Construction ---
    def _create_filter_panel(self):
        grp = QGroupBox("Filters")
        layout = QHBoxLayout()        
        layout.addWidget(QLabel("Show:"))        
        self.optAll = QRadioButton("All")
        self.optTBA = QRadioButton("TBA Only")
        self.optAll.setChecked(True)
        layout.addWidget(self.optAll)
        layout.addWidget(self.optTBA)        
        layout.addSpacing(20)
        layout.addWidget(QLabel("Media:"))        
        self.cmbMedia = QComboBox()
        self.cmbMedia.setMinimumWidth(150)
        layout.addWidget(self.cmbMedia)        
        layout.addStretch()
        layout.addWidget(make_help_button(self, "dashboard"))
        grp.setLayout(layout)
        return grp
        
    def _create_urgent_samples_group(self):
        grp = QGroupBox("Urgent Samples")
        layout = QVBoxLayout()        
        self.urgent_samples_table = QTableView()
        self.urgent_samples_table.setModel(self.urgent_samples_model)
        self._apply_table_styles(self.urgent_samples_table)
        layout.addWidget(self.urgent_samples_table)        
        grp.setLayout(layout)
        return grp
        
    def _create_active_batches_group(self):
        grp = QGroupBox("Active Batches")
        layout = QVBoxLayout()
        self.active_batches_table = QTableView()
        self.active_batches_table.setModel(self.active_batches_model)
        self._apply_table_styles(self.active_batches_table)
        layout.addWidget(self.active_batches_table)
        grp.setLayout(layout)
        return grp
        
    def _create_chart_widget(self):
        """Returns the chart widget directly (no GroupBox — title comes from matplotlib)."""
        self.chart_widget = MatplotlibChartWidget()
        return self.chart_widget
        
    def _create_status_summary_group(self):
        grp = QGroupBox("Status Summary")
        layout = QVBoxLayout()        
        self.status_summary_table = QTableView()
        self.status_summary_table.setModel(self.status_summary_model)
        self._apply_table_styles(self.status_summary_table)
        layout.addWidget(self.status_summary_table)        
        grp.setLayout(layout)
        return grp

    # --- Data Loading ---
    def refresh_dashboard(self):
        """Main refresh logic called by the launcher and filters."""
        try:
            media_id = self.cmbMedia.currentData()
            is_tba = self.optTBA.isChecked()
            prefix = "J" if db_manager.dialect == "SQL_SERVER" else "P"
            
            self.load_urgent_samples_table(media_id, is_tba)
            self.load_active_batches_table(media_id, prefix)
            self.load_status_summary_table(media_id)
            self.load_status_chart(media_id)
            
            self.status_label.setText(f"Last refreshed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            logging.error(f"Dashboard refresh error: {e}", exc_info=True)
            self.status_label.setText(f"Error: {str(e)}")
            
    def load_media_combo(self):
        self.cmbMedia.clear()
        self.cmbMedia.addItem("-- All Media --", None)
        try:
            with db_manager.get_connection() as conn:
                for row in conn.execute(text("""SELECT MediaID, medianame FROM Media
                                             WHERE MediaID IN (1, 200, 58, 201, 202)
                                             ORDER BY medianame
                                             """)):
                    self.cmbMedia.addItem(row.medianame, row.mediaid)
        except Exception as e:
            logging.error(f"Failed to load media: {e}")

    def _sql_concat(self, *args):
        dialect = getattr(db_manager, 'dialect', 'SQL_SERVER')
        parts = []
        for arg in args:
            if isinstance(arg, str) and arg.startswith("'"):
                parts.append(arg)
            else:
                if dialect == "SQL_SERVER":
                    parts.append(f"CAST({arg} AS NVARCHAR(255))")
                else:
                    parts.append(str(arg))
        sep = " + " if dialect == "SQL_SERVER" else " || "
        return sep.join(parts)

    def _execute_and_populate_table(self, sql, params, model, headers):
        model.clear()
        model.setHorizontalHeaderLabels(headers)
        try:
            with db_manager.get_connection() as conn:
                for row in conn.execute(text(sql), params):
                    items = []
                    for s in row:
                        items.append(QStandardItem(str(s) if s is not None else ""))
                    model.appendRow(items)
        except Exception as e:
            logging.error(f"Dashboard query failed: {e}")
            
    def load_urgent_samples_table(self, media_id, is_tba):
        headers = ["Submission ID", "OurLabID", "Priority", "Workflow", "Status"]
        params = {}
        _ourlabid = db_manager.sql_concat('Sample.Prefix', "'-'", 'CAST(Sample.SampleID AS VARCHAR)')
        
        if is_tba:
            sql = f"""SELECT Submission.SubmissionID, {db_manager.sql_top(100)}{_ourlabid} AS OurLabID, Priority.Description, Workflow.Abbreviation, Sample.Status
                FROM public.sample s
                INNER JOIN public.sample_queue sq ON sq.sampleid = s.sampleid AND sq.prefix = s.prefix
                INNER JOIN public.workflowjob wj ON wj.workflowjobid = sq.workflowjobid
                INNER JOIN public.workflow Workflow ON Workflow.workflowid = wj.workflowid
                INNER JOIN public.submission Submission ON Submission.submissionid = s.submissionid
                INNER JOIN public.priority Priority ON Priority.priorityid = Submission.priorityid
                INNER JOIN public.sample Sample ON Sample.sampleid = s.sampleid AND Sample.prefix = s.prefix
                WHERE Submission.SubmissionType <> 1"""
            if media_id is not None:
                sql += " AND (sq.mediaid = :mid)"; params["mid"] = media_id
        else:
            sql = f"""SELECT Submission.SubmissionID,{db_manager.sql_top(100)}{_ourlabid} AS OurLabID, Priority.Description, Workflow.Abbreviation, Sample.Status
                FROM Sample
                INNER JOIN Submission ON Submission.SubmissionID = Sample.SubmissionID
                INNER JOIN Priority ON Submission.PriorityID = Priority.PriorityID
                LEFT JOIN Workflow ON Workflow.WorkflowID = Sample.WorkflowID
                LEFT JOIN public.sample_queue sq ON sq.sampleid = Sample.SampleID AND sq.prefix = Sample.Prefix
                LEFT JOIN Analysis ON (Sample.SampleID=Analysis.SampleID AND Sample.Prefix=Analysis.Prefix)
                WHERE (Analysis.SampleID IS NULL) AND (sq.queue_id IS NULL) AND (Submission.SubmissionType <> 1)"""
            if media_id is not None:
                sql += " AND (Sample.MediaID = :mid)"; params["mid"] = media_id

        sql += " ORDER BY Priority.PriorityID, Submission.SubmissionDate, Sample.SampleID"
        sql += db_manager.sql_limit(100)
        self._execute_and_populate_table(sql, params, self.urgent_samples_model, headers)
        self.urgent_samples_table.resizeColumnsToContents()
        self.urgent_samples_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)

    def load_active_batches_table(self, media_id, prefix):
        """
        Combined ongoing-run table.  One row per active batch from any run type.
        Each sub-query is wrapped in its own try-except so missing schemas are
        silently skipped (e.g. a TRIMS-only lab won't have SIAM, and vice-versa).
        """
        headers = ["WorkflowJob", "Run ID", "Procedure", "Equipment",
                   "Start Time", "Samples"]
        self.active_batches_model.clear()
        self.active_batches_model.setHorizontalHeaderLabels(headers)

        isnull  = "COALESCE" if db_manager.dialect == "POSTGRESQL" else "ISNULL"
        pg      = db_manager.dialect == "POSTGRESQL"
        bool_f  = db_manager.sql_bool(False)

        def _dt(col):
            return (f"TO_CHAR({col}, 'YYYY-MM-DD HH24:MI')" if pg
                    else f"FORMAT({col}, 'yyyy-MM-dd HH:mm')")

        def _rows(rs, job):
            """Prepend WorkflowJob then append the remaining DB columns."""
            for r in rs:
                self.active_batches_model.appendRow(
                    [QStandardItem(job)]
                    + [QStandardItem(str(v) if v is not None else "") for v in r]
                )

        try:
            with db_manager.get_connection() as conn:

                # ── 1. Distillation (TRIMS.PrimaryDistillationBatch) ──────────
                try:
                    dt = "TO_CHAR(b.StartDate,'YYYY-MM-DD')" if pg else "FORMAT(b.StartDate,'yyyy-MM-dd')"
                    rs = conn.execute(text(f"""
                        SELECT b.RunID, '', {dt},
                               COUNT(pd.AnalysisID)
                        FROM TRIMS.PrimaryDistillationBatch b
                        LEFT JOIN TRIMS.PrimaryDistillation pd ON pd.RunID = b.RunID
                        WHERE b.IsLocked = {bool_f} AND b.EndDate IS NULL
                        GROUP BY b.RunID, b.StartDate
                        ORDER BY b.StartDate DESC
                    """))
                    _rows(rs, "Distillation")
                except Exception as ex:
                    logging.debug("Distillation query skipped: %s", ex)

                # ── 2. Electrolysis (TRIMS.ElectrolysisRun, RunStatus=4) ──────
                try:
                    rs = conn.execute(text(f"""
                        SELECT er.RunID, ap.ProcedureName, eq.EquipmentName,
                               {_dt('er.RunStartTime')}, 0
                        FROM TRIMS.ElectrolysisRun er
                        LEFT JOIN AnalysisProcedure ap ON ap.ProcedureID = er.ProcedureID
                        LEFT JOIN Equipment eq ON eq.EquipmentID = er.ElysSystemID
                        WHERE er.RunStatus = 4
                        ORDER BY er.RunStartTime DESC
                    """))
                    _rows(rs, "Electrolysis")
                except Exception as ex:
                    logging.debug("Electrolysis query skipped: %s", ex)

                # ── 3. Chemical Enrichment (TRIMS.ChemEnrRun, IsFinished=0) ───
                try:
                    dt = "TO_CHAR(r.RunDate,'YYYY-MM-DD')" if pg else "FORMAT(r.RunDate,'yyyy-MM-dd')"
                    rs = conn.execute(text(f"""
                        SELECT r.RunID, m.MeasurableName, '',
                               {dt}, {isnull}(cnt.n, 0)
                        FROM TRIMS.ChemEnrRun r
                        LEFT JOIN Measurables m ON m.MeasurableID = r.MeasurableID
                        LEFT JOIN (
                            SELECT RunID, COUNT(ChemEnrID) AS n
                            FROM TRIMS.ChemicalEnrichment GROUP BY RunID
                        ) cnt ON cnt.RunID = r.RunID
                        WHERE r.IsFinished = {bool_f}
                        ORDER BY r.RunDate DESC
                    """))
                    _rows(rs, "Enrichment")
                except Exception as ex:
                    logging.debug("Enrichment query skipped: %s", ex)

                # ── 4. LSC Runs (TRIMS.LSCRun, RunStatus=6) ──────────────────
                try:
                    rs = conn.execute(text(f"""
                        SELECT l.RunID, ap.ProcedureName, eq.EquipmentName,
                               {_dt('l.RunStartTime')}, {isnull}(cnt.noSamples, 0)
                        FROM TRIMS.LSCRun l
                        INNER JOIN Equipment eq ON eq.EquipmentID = l.EquipmentID
                        INNER JOIN AnalysisProcedure ap ON ap.ProcedureID = l.ProcedureID
                        LEFT JOIN (
                            SELECT RunID, COUNT(CountID) AS noSamples
                            FROM TRIMS.LSCLoadList GROUP BY RunID
                        ) cnt ON l.RunID = cnt.RunID
                        WHERE l.RunStatus = 6
                        ORDER BY l.RunStartTime DESC
                    """))
                    _rows(rs, "LSC Run")
                except Exception as ex:
                    logging.debug("LSC query skipped: %s", ex)

                # ── 5. Chemistry Batches (ChemistryBatch, RunEndTime IS NULL) ─
                try:
                    params: dict = {}
                    sql = f"""
                        SELECT cb.RunID, ap.ProcedureName, eq.EquipmentName,
                               {_dt('cb.RunStartTime')}, 0
                        FROM ChemistryBatch cb
                        INNER JOIN AnalysisProcedure ap ON ap.ProcedureID = cb.ProcedureID
                        INNER JOIN Equipment eq ON eq.EquipmentID = cb.EquipmentID
                        INNER JOIN Workflow w ON w.WorkflowID = cb.WorkflowID
                        WHERE cb.RunEndTime IS NULL
                    """
                    if media_id is not None:
                        sql += " AND w.MediaID = :mid"; params["mid"] = media_id
                    sql += " ORDER BY cb.RunStartTime DESC"
                    _rows(conn.execute(text(sql), params), "Chemistry Batch")
                except Exception as ex:
                    logging.debug("ChemistryBatch query skipped: %s", ex)

                # ── 6. SI Analysis Runs (SIAM.SIAnalysisRun, RunStatus<8) ─────
                try:
                    params = {}
                    eq_name = db_manager.sql_concat("eq.Identifier", "'-'", "eq.EquipmentName")
                    sql = f"""
                        SELECT sa.SIAnalysisRunID, ap.ProcedureName, {eq_name},
                               {_dt('sa.RunStartTime')}, {isnull}(cnt.noSamples, 0)
                        FROM SIAM.SIAnalysisRun sa
                        INNER JOIN Equipment eq ON eq.EquipmentID = sa.EquipmentID
                        INNER JOIN AnalysisProcedure ap ON ap.ProcedureID = sa.ProcedureID
                        INNER JOIN Workflow w ON w.WorkflowID = sa.WorkflowID
                        LEFT JOIN (
                            SELECT SIAnalysisRunID, COUNT(PositionInRun) AS noSamples
                            FROM SIAM.SIAnalysisLoadList GROUP BY SIAnalysisRunID
                        ) cnt ON sa.SIAnalysisRunID = cnt.SIAnalysisRunID
                        WHERE sa.RunStatus < 8
                    """
                    if media_id is not None:
                        sql += " AND w.MediaID = :mid"; params["mid"] = media_id
                    sql += " ORDER BY sa.RunStartTime DESC"
                    _rows(conn.execute(text(sql), params), "SI Analysis")
                except Exception as ex:
                    logging.debug("SI Analysis query skipped: %s", ex)

        except Exception as e:
            logging.error("load_active_batches_table outer error: %s", e)

        self.active_batches_table.resizeColumnsToContents()
        self.active_batches_table.setColumnWidth(1, 60)
        self.active_batches_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        
    def load_status_summary_table(self, media_id):
        headers = ["Media", "Status", "Sample Count"]
        params = {}
        sql = f"""
            SELECT t0.MediaName, t0.Description, Count(t0.SampleID)
            FROM (SELECT DISTINCT vwSampleAnalysisStatus.Prefix, SampleID, vwSampleAnalysisStatus.Status, vwSampleAnalysisStatus.MediaID, vwSampleAnalysisStatus.Description, Media.abbreviation AS MediaName
                  FROM vwSampleAnalysisStatus
                  INNER JOIN Submission ON vwSampleAnalysisStatus.SubmissionID = Submission.SubmissionID
                  INNER JOIN Media ON Media.MediaID = vwSampleAnalysisStatus.MediaID
                  WHERE Submission.SubmissionType <> 1
        """
        if media_id is not None:
            sql += " AND vwSampleAnalysisStatus.MediaID = :mid"; params["mid"] = media_id
        sql += ") AS t0 GROUP BY t0.MediaID, t0.MediaName, t0.Status, t0.Description ORDER BY t0.MediaName, t0.Status"
        self._execute_and_populate_table(sql, params, self.status_summary_model, headers)
        self.status_summary_table.resizeColumnsToContents()
        self.status_summary_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)


    def load_status_chart(self, media_id):
        """
        All Media  → stacked horizontal bar (one row per media, segments = phase bucket)
        Single media → two donuts side-by-side (phase aggregate + status detail)
        """
        params = {}
        sql = """
            SELECT Media.abbreviation,
                   vwSampleAnalysisStatus.phase_label,
                   vwSampleAnalysisStatus.description,
                   COUNT(DISTINCT vwSampleAnalysisStatus.SampleID) AS cnt
            FROM vwSampleAnalysisStatus
            INNER JOIN Submission ON vwSampleAnalysisStatus.SubmissionID = Submission.SubmissionID
            INNER JOIN Media      ON Media.MediaID = vwSampleAnalysisStatus.MediaID
            WHERE Submission.SubmissionType <> 1
        """
        if media_id is not None:
            sql += " AND vwSampleAnalysisStatus.MediaID = :mid"
            params["mid"] = media_id
        sql += """ GROUP BY Media.abbreviation, Media.MediaID,
                            vwSampleAnalysisStatus.phase, vwSampleAnalysisStatus.phase_label,
                            vwSampleAnalysisStatus.description
                   ORDER BY Media.MediaID, vwSampleAnalysisStatus.phase"""

        rows = []
        try:
            with db_manager.get_connection() as conn:
                for row in conn.execute(text(sql), params):
                    rows.append((row[0] or "Unknown", row[1] or "Ongoing", row[2] or "", row[3] or 0))
        except Exception as e:
            logging.error(f"Chart query failed: {e}")
            self.chart_widget.figure.clear()
            self.chart_widget.canvas.draw()
            return

        if not rows:
            self.chart_widget.figure.clear()
            self.chart_widget.figure.text(0.5, 0.5, "No data", ha='center', va='center',
                                          fontsize=13, color='#666')
            self.chart_widget.canvas.draw()
            return

        if media_id is None:
            # All Media: stacked bar
            self.chart_widget.plot_all_media(rows)
        else:
            # Single media: double donut
            media_name = rows[0][0] if rows else "Selected media"
            self.chart_widget.plot_single_media(rows, media_name)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    win = DashboardWidget()
    win.show()
    sys.exit(app.exec_())