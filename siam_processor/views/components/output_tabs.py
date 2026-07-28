from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QPushButton, QComboBox, QTabWidget, QTableWidget,
)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import logging


class OutputTabsBuilder:
    """
    Constructs the right-side output area of ProcessorWidget:
      • setup_output_tabs()     — data tables + Plots tab
      • setup_postprocess_tab() — IRMS post-processing sub-tabs

    All widgets are attached directly to ``self.parent`` (the ProcessorWidget)
    so every existing reference (e.g. ``self.injection_table``) keeps working.
    Store the builder as ``self._output_builder`` on the parent so it stays alive.
    """

    def __init__(self, parent):
        self.parent = parent

    # ------------------------------------------------------------------ #
    # Public entry points (mirror old method names exactly)              #
    # ------------------------------------------------------------------ #

    def setup_output_tabs(self):
        p = self.parent
        p.output_tabs = QTabWidget()

        # Raw / Injection Data tab
        p.injection_data_tab = QWidget()
        inj_layout = QVBoxLayout(p.injection_data_tab)
        p.injection_table = QTableWidget()
        inj_layout.addWidget(p.injection_table)
        p.output_tabs.addTab(p.injection_data_tab, "Raw / Injection Data")

        # Analysis Level Data tab
        p.analysis_data_tab = QWidget()
        ana_layout = QVBoxLayout(p.analysis_data_tab)
        p.analysis_table = QTableWidget()
        ana_layout.addWidget(p.analysis_table)
        p.output_tabs.addTab(p.analysis_data_tab, "Analysis Level Data")

        # Standards and Controls tab
        p.standards_tab = QWidget()
        std_layout = QVBoxLayout(p.standards_tab)
        p.standards_table = QTableWidget()
        std_layout.addWidget(p.standards_table)
        p.output_tabs.addTab(p.standards_tab, "Standards and Controls")

        self._setup_plot_tab()
        p.output_tabs.currentChanged.connect(p._on_output_tab_changed)

    def setup_postprocess_tab(self):
        """
        Create the Post-Processing tab with sub-tabs:
          Results | QC Standards | Batch Summary | Blanks/Carryover | Control Stats
        Safe to call multiple times (re-mounts if already built).
        """
        p = self.parent
        host = p._get_or_create_output_tabs()

        if getattr(p, "post_tab", None) is not None:
            # Already built — just make sure it's mounted
            try:
                if host.indexOf(p.post_tab) == -1:
                    host.addTab(p.post_tab, "Post-Processing")
            except Exception:
                pass

        p.post_tab = QWidget(p)
        outer = QVBoxLayout(p.post_tab)
        p.post_tabs = QTabWidget(p.post_tab)
        outer.addWidget(p.post_tabs)

        # Results
        p.post_results_table = QTableWidget(p.post_tab)
        res_wrap = QWidget(p.post_tab)
        res_layout = QVBoxLayout(res_wrap)
        res_layout.setContentsMargins(0, 0, 0, 0)
        res_layout.addWidget(p.post_results_table)
        p.post_tabs.addTab(res_wrap, "Results")

        # QC Standards
        p.qc_table = QTableWidget(p.post_tab)
        qc_wrap = QWidget(p.post_tab)
        qc_layout = QVBoxLayout(qc_wrap)
        qc_layout.setContentsMargins(0, 0, 0, 0)
        qc_layout.addWidget(p.qc_table)
        p.post_tabs.addTab(qc_wrap, "QC Standards")

        # Batch Summary
        p.batch_summary_table = QTableWidget(p.post_tab)
        bs_wrap = QWidget(p.post_tab)
        bs_layout = QVBoxLayout(bs_wrap)
        bs_layout.setContentsMargins(0, 0, 0, 0)
        bs_layout.addWidget(p.batch_summary_table)
        p.post_tabs.addTab(bs_wrap, "Batch Summary")

        # Blanks / Carryover (two stacked tables)
        diag_wrap = QWidget(p.post_tab)
        diag_layout = QVBoxLayout(diag_wrap)
        diag_layout.setContentsMargins(0, 0, 0, 0)
        diag_layout.addWidget(QLabel("Blank diagnostics"))
        p.blank_diag_table = QTableWidget(p.post_tab)
        diag_layout.addWidget(p.blank_diag_table)
        diag_layout.addWidget(QLabel("Top carryover candidates"))
        p.carryover_table = QTableWidget(p.post_tab)
        diag_layout.addWidget(p.carryover_table)
        p.post_tabs.addTab(diag_wrap, "Blanks / Carryover")

        # Control Stats
        p.control_stats_table = QTableWidget(p.post_tab)
        cs_wrap = QWidget(p.post_tab)
        cs_layout = QVBoxLayout(cs_wrap)
        cs_layout.setContentsMargins(0, 0, 0, 0)
        cs_layout.addWidget(p.control_stats_table)
        p.post_tabs.addTab(cs_wrap, "Control Stats")

        # Mount and hide until IRMS data is available
        host.addTab(p.post_tab, "Post-Processing")
        for attr in ("btn_run_post", "btn_export_post", "btn_edit_config"):
            if hasattr(p, attr):
                getattr(p, attr).setVisible(False)

        p.reset_postprocess(hide_tab=True)

        for attr in ("btn_run_post", "btn_export_post", "btn_edit_config"):
            if hasattr(p, attr):
                getattr(p, attr).setVisible(False)

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    def _setup_plot_tab(self):
        p = self.parent
        p.plot_tab = QWidget()
        main_plot_layout = QVBoxLayout(p.plot_tab)

        controls_group = QGroupBox("Plot Controls")
        controls_layout = QVBoxLayout()

        p.plot_combo = QComboBox()
        controls_layout.addWidget(QLabel("Select Plot Type:"))
        controls_layout.addWidget(p.plot_combo)
        p.plot_combo.currentTextChanged.connect(lambda _=None: p._schedule_auto_plot())

        p.dynamic_controls_widget = QWidget()
        p.dynamic_controls_layout = QHBoxLayout(p.dynamic_controls_widget)
        p.analysis_combo_label = QLabel("Analysis ID:")
        p.analysis_combo = QComboBox()
        p.analysis_combo.currentTextChanged.connect(lambda _=None: p._schedule_auto_plot())
        p.dynamic_controls_layout.addWidget(p.analysis_combo_label)
        p.dynamic_controls_layout.addWidget(p.analysis_combo)
        controls_layout.addWidget(p.dynamic_controls_widget)
        p.dynamic_controls_widget.hide()

        button_layout = QHBoxLayout()
        p.canvas_plot_btn = QPushButton("Plot on Canvas (Static)")
        p.canvas_plot_btn.clicked.connect(p.plot_on_canvas)
        p.browser_plot_btn = QPushButton("Plot in Browser (Interactive)")
        p.browser_plot_btn.clicked.connect(p.open_plot_in_browser)
        button_layout.addWidget(p.canvas_plot_btn)
        button_layout.addWidget(p.browser_plot_btn)
        controls_layout.addLayout(button_layout)

        p.generate_reports_button = QPushButton("Generate & Open Full Report Folder")
        p.generate_reports_button.clicked.connect(p.generate_and_open_reports)
        controls_layout.addWidget(p.generate_reports_button)

        p.plot_status_label = QLabel("Validation results: Not processed")
        p.plot_status_label.setWordWrap(True)
        p.plot_status_label.setStyleSheet("color: #444; font-size: 16px;")
        controls_layout.addWidget(p.plot_status_label)

        controls_group.setLayout(controls_layout)

        p.figure = Figure(constrained_layout=True)
        p.canvas = FigureCanvas(p.figure)
        main_plot_layout.addWidget(controls_group)
        main_plot_layout.addWidget(p.canvas)

        p.plot_combo.currentTextChanged.connect(p.on_plot_selection_changed)
        p.output_tabs.addTab(p.plot_tab, "Plots & Reports")
        p.output_tabs.setTabEnabled(3, False)
