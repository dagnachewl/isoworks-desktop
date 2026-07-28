# -*- mode: python ; coding: utf-8 -*-
#
# IsoWorks.spec — PyInstaller build spec
#
# Build (from the project root, with the isoworks conda env active):
#   conda activate isoworks
#   pip install pyinstaller          # first time only
#   pyinstaller IsoWorks.spec
#
# Output:
#   dist/IsoWorks/          – distributable folder  (Windows / Linux)
#   dist/IsoWorks.app/      – .app bundle           (macOS, with BUNDLE block)
#
# To create a single-file executable instead of a folder, change
# exe_onefile = False  →  True  below (slower startup, but one file to ship).
#
# ---------------------------------------------------------------------------

exe_onefile = False          # True = single binary (slow start); False = folder

# ---------------------------------------------------------------------------
# Collect data + binaries from packages that use dynamic internal imports
# ---------------------------------------------------------------------------
from PyInstaller.utils.hooks import collect_all, collect_submodules, collect_data_files

mpl_d,   mpl_b,   mpl_h   = collect_all('matplotlib')
scipy_d, scipy_b, scipy_h = collect_all('scipy')
pd_d,    pd_b,    pd_h    = collect_all('pandas')
# local sub-packages — collected explicitly so PyInstaller sees them all
lsc_d,   lsc_b,   lsc_h   = collect_all('trims_lsc')
irms_d,  irms_b,  irms_h  = collect_all('irms')
iso_d,   iso_b,   iso_h   = collect_all('iso_io')
ui_d,    ui_b,    ui_h    = collect_all('ui')

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    ['launcher.py'],
    pathex=['.'],

    # ── extra binaries ──────────────────────────────────────────────────────
    binaries=(
        mpl_b + scipy_b + pd_b +
        lsc_b + irms_b + iso_b + ui_b
    ),

    # ── non-Python data files ───────────────────────────────────────────────
    datas=[
        # JSON form definitions read at runtime
        ('forms',                          'forms'),
        # HTML report template
        ('reports/isotope_report.html',    'reports'),
        # Help docs (if referenced at runtime)
        ('docs',                           'docs'),
    ] + mpl_d + scipy_d + pd_d + lsc_d + irms_d + iso_d + ui_d,

    # ── hidden imports ──────────────────────────────────────────────────────
    # Everything imported lazily inside functions in launcher.py + transitive deps
    hiddenimports=[

        # ── launcher lazy-loaded modules ─────────────────────────────────
        'settings_module',
        'project_list_gui',
        'sample_submission_gui',
        'samples_tba_management_gui',
        'sample_workflow_assign_gui',
        'siam_run_list_gui',
        'siam_preanalysis_runs_gui',
        'siam_preanalysis_details_gui',
        'siam_preanalysis_create_gui',
        'dashboard_gui',
        'siam_processor.views.main_window',
        'siam_create_run_gui',
        'siam_run_details_gui',
        'siam_processor_config_gui',
        'siam_finalize_dialog',
        'siam_template_editor_gui',
        'siam_column_resolver',
        'trims_distillation_runs_gui',
        'trims_distillation_details_gui',
        'trims_distillation_create_run_gui',
        'trims_electrolysis_runs_gui',
        'trims_electrolysis_details_gui',
        'trims_electrolysis_create_run_gui',
        'trims_lsc_runs_gui',
        'trims_lsc_details_gui',
        'trims_lsc_create_run_gui',
        'trims_chemical_enrichment_runs_gui',
        'trims_chemical_enrichment_gui',
        'trims_hidex_lsc_parser',
        'ngam_ingrowth_runs_gui',
        'ngam_ingrowth_run_gui',
        'ngam_ingrowth_create_run_gui',
        'ngam_extraction_runs_gui',
        'ngam_extraction_run_gui',
        'ngam_extraction_create_run_gui',
        'ngam_ng_sequence_runs_gui',
        'ngam_ng_sequence_run_gui',
        'ngam_sequence_runs_gui',
        'ngam_sequence_run_gui',
        'ngam_import_from_protocol_gui',
        'ngam_ms_import_gui',
        'ngam_ms_parser',
        'ngam_ms_processor',
        'ngam_ng_parser',
        'ngam_ng_processor',
        'ngam_ng_template_editor',
        'ngam_protocol_parser',
        'employee_management_gui',
        'customer_management_gui',
        'equipment_management_gui',
        'equipment_maintenance_dialog_gui',
        'procedure_management_gui',
        'reference_management_gui',
        'reference_data_subform_gui',
        'workflow_management_gui',
        'workflow_jobs_dialog_gui',
        'workflow_job_editor_dialog_gui',
        'global_parameters_gui',
        'enrichment_config_gui',
        'enrichment_template_editor_gui',
        'lsc_template_editor_gui',
        'lsc_protocol_gui',
        'lsc_protocol_manager',
        'qaqc_gui',
        'submission_details_gui',
        'submission_report_gui',
        'sample_mgmt_import',
        'protocol_gui',
        'protocol_manager',
        'outliers',
        'plots',
        'smart_formatting',
        'shared_utils',
        'gui_utils',
        'db_utils',
        'database',
        'db_connector',
        'animated_sidebar',
        'auto_sampler_tray_layout',
        'visual_tray_widgets',
        'ui_delegates',
        'dlg_primary_distillation_ec',
        'dymo_utils',
        'isotope_processor',
        'laser_role_validator',
        'help_browser',
        'validate_help_anchors',

        # ── trims_lsc sub-package ────────────────────────────────────────
        'trims_lsc',
        'trims_lsc.computation',
        'trims_lsc.computation.activity',
        'trims_lsc.computation.statistics',
        'trims_lsc.gui',
        'trims_lsc.gui.helpers',
        'trims_lsc.gui.main_dialog',
        'trims_lsc.gui.run_details_window',
        'trims_lsc.parsers',
        'trims_lsc.parsers.aloka',
        'trims_lsc.parsers.base',
        'trims_lsc.parsers.factory',
        'trims_lsc.parsers.hidex',
        'trims_lsc.parsers.other',
        'trims_lsc.parsers.packard',
        'trims_lsc.parsers.quantulus',
        'trims_lsc.workers',
        'trims_lsc.workers.import_worker',

        # ── irms sub-package ─────────────────────────────────────────────
        'irms', 'irms.api', 'irms.base_reader', 'irms.cli_ea', 'irms.registry',
        'irms.processing', 'irms.processing.calibration', 'irms.processing.irms_tools',
        'irms.readers', 'irms.readers.elemental_cf_text',
        'irms.readers.parse_thermo_dualinlet_xls', 'irms.readers.thermo_cf_text',
        'irms.readers.thermo_dual_inlet', 'irms.readers.thermo_dual_inlet_csv',
        'irms.readers.thermo_dual_inlet_xls',
        'irms.utils', 'irms.utils.normalize', 'irms.utils.textio', 'irms.utils.xmlini',

        # ── iso_io sub-package ───────────────────────────────────────────
        'iso_io', 'iso_io.irms_reader', 'iso_io.laser_reader',

        # ── ui sub-package ───────────────────────────────────────────────
        'ui', 'ui.plots_tab', 'ui.post_tab', 'ui.qt_helpers', 'ui.tables',

        # ── psycopg2 (C extension — static analysis misses it) ───────────
        'psycopg2',
        'psycopg2._psycopg',
        'psycopg2.extensions',
        'psycopg2.extras',
        'psycopg2.errors',

        # ── SQLAlchemy dialects ──────────────────────────────────────────
        'sqlalchemy.dialects.postgresql',
        'sqlalchemy.dialects.postgresql.psycopg2',

        # ── matplotlib backend ───────────────────────────────────────────
        'matplotlib.backends.backend_qt5agg',
        'matplotlib.backends.backend_agg',
        'matplotlib.backends.backend_pdf',

        # ── PyQt5 extras ─────────────────────────────────────────────────
        'PyQt5.QtPrintSupport',
        'PyQt5.QtWebEngineWidgets',
        'PyQt5.QtSvg',

        # ── stdlib / misc that get missed ────────────────────────────────
        'pkg_resources',
        'openpyxl',
        'openpyxl.cell._writer',
        'PIL._tkinter_finder',

    ] + mpl_h + scipy_h + pd_h + lsc_h + irms_h + iso_h + ui_h,

    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Access / SQL Server drivers — not used in PostgreSQL deployments
        'pyodbc',
        'sqlalchemy.dialects.mssql',
        'sqlalchemy.dialects.oracle',
        # Unused heavy packages
        'tkinter', '_tkinter',
        'IPython', 'jupyter',
        'PyQt5.QtWebEngine',      # comment out if help_browser uses WebEngine
    ],
    noarchive=False,
    optimize=0,
)

# ---------------------------------------------------------------------------
# PYZ archive
# ---------------------------------------------------------------------------
pyz = PYZ(a.pure)

# ---------------------------------------------------------------------------
# EXE
# ---------------------------------------------------------------------------
exe = EXE(
    pyz,
    a.scripts,
    a.binaries    if exe_onefile else [],
    a.datas       if exe_onefile else [],
    exclude_binaries=not exe_onefile,
    name='IsoWorks',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,             # UPX compression breaks some PyQt5 builds; leave off
    console=False,         # no terminal window (windowed app)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,      # None = native arch; set 'x86_64' for Intel-only
    codesign_identity=None,
    entitlements_file=None,
    # icon='path/to/isoworks.icns',   # ← add macOS/Windows icon here
)

# ---------------------------------------------------------------------------
# COLLECT (folder mode only — ignored when exe_onefile = True)
# ---------------------------------------------------------------------------
if not exe_onefile:
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name='IsoWorks',
    )

# ---------------------------------------------------------------------------
# BUNDLE — macOS .app (comment out on Windows / Linux)
# ---------------------------------------------------------------------------
import sys
if sys.platform == 'darwin':
    app = BUNDLE(
        coll if not exe_onefile else exe,
        name='IsoWorks.app',
        # icon='path/to/isoworks.icns',
        bundle_identifier='org.isoworks.pylims',
        info_plist={
            'NSPrincipalClass':              'NSApplication',
            'NSHighResolutionCapable':       True,
            'CFBundleShortVersionString':    '1.0.0',
            'CFBundleVersion':              '1.0.0',
            'NSHumanReadableCopyright':     'IsoWorks pyLIMS',
        },
    )
