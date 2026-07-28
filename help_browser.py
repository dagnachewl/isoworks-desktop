"""
help_browser.py
===============
Context-sensitive help browser for IsoWorks pyLIMS.

Usage
-----
From any module, call::

    from help_browser import show_help
    show_help(parent_widget, "siam_run_list")   # module key OR raw anchor

Or use the convenience button factory::

    from help_browser import make_help_button
    btn = make_help_button(self, "trims_lsc")
    toolbar_layout.addWidget(btn)

Topic keys map to anchors in IsoWorks_pyLIMS_User_Manual.md.
Add new keys to HELP_TOPICS as new modules are created.
"""
from __future__ import annotations

import os
import logging

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
    QTextBrowser, QLineEdit, QLabel, QWidget,
)
from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QKeySequence

# ── Manual location ────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_MANUAL_MD = os.path.join(_HERE, "Manuals", "IsoWorks_pyLIMS_User_Manual.md")

# ── Topic → anchor mapping ─────────────────────────────────────────────────────
# Keys match the module keys defined in launcher.py.
# Values are GFM anchors generated from Markdown headings:
#   lowercase, spaces→hyphens, remove all punctuation except hyphens.
# Section headings carry no numeric prefix; anchors match the heading text directly.
HELP_TOPICS: dict[str, str] = {
    # Top-level
    "intro":                        "1-introduction",
    "dashboard":                    "3-dashboard",
    # Sample management
    "import_submission":            "41-import-new-submission",
    "submission_list":              "42-submission-management",
    "workflow_assign":              "43-stage-samples-to-tba",
    "samples_tba":                  "44-manage-analysis-queue",
    # SIAM
    "siam_preanalysis":             "51-pre-analysis-batches-major-ions-nox-screening",
    "siam_run_list":                "52-siam-runs",
    "siam_create_run":              "creating-a-new-siam-run-batch-setup",
    "create_siam_run_processor":    "53-process-run-file-based-processor",
    # TRIMS
    "trims_distillation":           "61-primary-distillation",
    "trims_create_distillation":    "creating-a-distillation-batch",
    "trims_enrichment":             "62-electrolytic-enrichment",
    "trims_create_enrichment":      "creating-an-enrichment-run",
    "trims_lsc":                    "63-lsc-runs",
    "trims_create_lsc":             "creating-an-lsc-run",
    "trims_lsc_eval":               "64-evaluation-finalizing-lsc-data",
    # NGAM
    "ngam_ingrowth":                "71-3he-ingrowth-runs",
    "ngam_create_ingrowth":         "creating-an-ingrowth-run",
    "ngam_extraction":              "72-3he-extraction-runs",
    "ngam_create_extraction":       "creating-an-extraction-run",
    "ngam_line_efficiency":         "extraction-line-efficiency-calibrations",
    "ngam_sequence":                "73-3he-measurement-runs-helix-sft",
    "ngam_create_sequence":         "creating-a-measurement-sequence",
    "ngam_ng_sequence":             "74-ng-ms-sequence-runs-noblecontrol-qtegra",
    "ngam_create_ng_sequence":      "creating-an-ng-sequence-run",
    "ngam_ng_import":               "importing-and-processing-ng-data",
    "ngam_eqw_cf":                  "75-eqw-correction-factors",
    # QA/QC
    "qaqc":                         "8-qaqc-module",
    # Settings
    "db_connection":                "91-database-connection",
    "employee_mgmt":                "92-employee-management",
    "customer_mgmt":                "93-customer-management",
    "equipment_mgmt":               "94-equipment-management",
    "procedure_mgmt":               "95-procedure-management",
    "workflow_mgmt":                "96-workflow-management",
    "reference_mgmt":               "97-references-controls",
    "global_params":                "98-global-parameters",
    # CIMS
    "cims":                         "110-consumables-inventory-management",
    # Appendices
    "privileges":                   "appendix-a-privilege-roles",
    "glossary":                     "appendix-b-glossary",
    "status_indicators":            "appendix-c-status-indicators",
    "file_formats":                 "appendix-d-supported-file-formats",
    "ngam_linearity_theory":        "appendix-f-ngam-linearity-gauge-calibration-technical-notes",
}

# ── HTML cache ─────────────────────────────────────────────────────────────────
_html_cache: str | None = None


def _strip_mermaid(text: str) -> str:
    """Replace ```mermaid blocks with a placeholder — QTextBrowser has no JS."""
    import re
    return re.sub(
        r"`{3}mermaid[^\n]*\n.*?`{3}",
        "\n> *[Diagram — open in VS Code (Cmd+Shift+V) to view]*\n",
        text,
        flags=re.DOTALL,
    )


def _md_to_html(md_path: str) -> str:
    """Convert the Markdown manual to HTML, with heading anchors."""
    global _html_cache
    if _html_cache is not None:
        return _html_cache

    try:
        import markdown
        with open(md_path, encoding="utf-8") as fh:
            text = fh.read()
        text = _strip_mermaid(text)
        # 'toc' extension generates the same anchor IDs used in HELP_TOPICS
        html_body = markdown.markdown(
            text,
            extensions=["toc", "tables", "fenced_code"],
        )
        _html_cache = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body  {{ font-family: "Segoe UI", Arial, sans-serif; font-size: 13px;
           line-height: 1.55; margin: 16px 20px; color: #1a1a2e; }}
  h1    {{ font-size: 1.5em; color: #0B3D91; border-bottom: 2px solid #0B3D91;
           padding-bottom: 4px; margin-top: 1.4em; }}
  h2    {{ font-size: 1.2em; color: #1a4a8a; border-bottom: 1px solid #c0d0e8;
           padding-bottom: 2px; margin-top: 1.2em; }}
  h3    {{ font-size: 1.05em; color: #2c5f9e; margin-top: 1em; }}
  h4    {{ font-size: 0.97em; color: #3a6aaa; margin-top: 0.8em; }}
  table {{ border-collapse: collapse; width: 100%; margin: 0.8em 0; font-size: 0.95em; }}
  th    {{ background: #e8eef8; color: #1a3a6a; text-align: left;
           padding: 5px 8px; border: 1px solid #b8c8e0; }}
  td    {{ padding: 4px 8px; border: 1px solid #d0dcea; vertical-align: top; }}
  tr:nth-child(even) td {{ background: #f5f8fd; }}
  code  {{ background: #f0f4f8; padding: 1px 4px; border-radius: 3px;
           font-family: "Consolas", monospace; font-size: 0.92em; color: #c0392b; }}
  pre   {{ background: #f0f4f8; padding: 10px; border-radius: 4px;
           font-size: 0.88em; overflow-x: auto; border: 1px solid #d0dcea; }}
  blockquote {{ border-left: 3px solid #3B82F6; margin: 0.8em 0;
                padding: 6px 12px; background: #eff6ff; color: #1e40af;
                border-radius: 0 4px 4px 0; }}
  a     {{ color: #1a6ac4; }}
  li    {{ margin-bottom: 3px; }}
  hr    {{ border: none; border-top: 1px solid #d0dcea; margin: 1.2em 0; }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""
        return _html_cache

    except ImportError:
        logging.warning("help_browser: 'markdown' package not found; showing plain text.")
        with open(md_path, encoding="utf-8") as fh:
            raw = fh.read()
        escaped = raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        _html_cache = f"<html><body><pre>{escaped}</pre></body></html>"
        return _html_cache

    except Exception as exc:
        logging.error("help_browser: failed to load manual: %s", exc)
        return f"<html><body><p><b>Could not load manual:</b> {exc}</p></body></html>"


# ── Dialog ─────────────────────────────────────────────────────────────────────

class HelpBrowserDialog(QDialog):
    """
    Floating help window showing the IsoWorks user manual.
    Scrolls to the section matching the given topic anchor.
    """

    # Singleton — reuse the same window across calls
    _instance: "HelpBrowserDialog | None" = None

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("IsoWorks pyLIMS — Help")
        self.resize(820, 680)
        self.setWindowFlags(
            Qt.Window | Qt.WindowMinimizeButtonHint |
            Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Search bar ──────────────────────────────────────────────────
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Find:"))
        self._search = QLineEdit()
        self._search.setPlaceholderText("Type to search in manual…")
        self._search.returnPressed.connect(self._find_next)
        bar.addWidget(self._search, 1)
        btn_prev = QPushButton("◀")
        btn_prev.setFixedWidth(28)
        btn_prev.setToolTip("Find previous")
        btn_prev.clicked.connect(self._find_prev)
        btn_next = QPushButton("▶")
        btn_next.setFixedWidth(28)
        btn_next.setToolTip("Find next")
        btn_next.clicked.connect(self._find_next)
        bar.addWidget(btn_prev)
        bar.addWidget(btn_next)
        root.addLayout(bar)

        # ── Browser ─────────────────────────────────────────────────────
        self._browser = QTextBrowser()
        self._browser.setOpenExternalLinks(False)
        self._browser.setOpenLinks(False)          # handle anchors ourselves
        self._browser.anchorClicked.connect(self._on_anchor_clicked)
        root.addWidget(self._browser, 1)

        # ── Bottom bar ──────────────────────────────────────────────────
        bot = QHBoxLayout()
        self._lbl_topic = QLabel("")
        self._lbl_topic.setStyleSheet("color: #666; font-size: 11px;")
        bot.addWidget(self._lbl_topic, 1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.hide)
        btn_close.setShortcut(QKeySequence("Escape"))
        bot.addWidget(btn_close)
        root.addLayout(bot)

        # Load HTML (cached after first call)
        html = _md_to_html(_MANUAL_MD)
        self._browser.setHtml(html)

    # ── Public API ──────────────────────────────────────────────────────────────

    def navigate_to(self, topic: str):
        """Scroll to the section matching *topic* (module key or raw anchor)."""
        anchor = HELP_TOPICS.get(topic, topic)
        self._browser.scrollToAnchor(anchor)
        self._lbl_topic.setText(f"Topic: {topic}  →  #{anchor}")

    # ── Internal ────────────────────────────────────────────────────────────────

    def _find_next(self):
        term = self._search.text()
        if term:
            self._browser.find(term)

    def _find_prev(self):
        from PyQt5.QtGui import QTextDocument
        term = self._search.text()
        if term:
            self._browser.find(term, QTextDocument.FindBackward)

    def _on_anchor_clicked(self, url: QUrl):
        frag = url.fragment()
        if frag:
            self._browser.scrollToAnchor(frag)
        else:
            import webbrowser
            webbrowser.open(url.toString())


# ── Public helpers ──────────────────────────────────────────────────────────────

def show_help(parent: QWidget | None, topic: str = "") -> None:
    """
    Open (or raise) the help window, scrolled to *topic*.

    Parameters
    ----------
    parent : QWidget or None
    topic  : module key (e.g. "siam_run_list") or raw anchor
             (e.g. "52-siam-runs"). Pass "" to open at the top.
    """
    if not os.path.isfile(_MANUAL_MD):
        from PyQt5.QtWidgets import QMessageBox
        QMessageBox.information(
            parent, "Help",
            f"Manual not found:\n{_MANUAL_MD}"
        )
        return

    if HelpBrowserDialog._instance is None or not HelpBrowserDialog._instance.isVisible():
        dlg = HelpBrowserDialog(parent)
        HelpBrowserDialog._instance = dlg

    dlg = HelpBrowserDialog._instance
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    if topic:
        dlg.navigate_to(topic)


def make_help_button(parent: QWidget, topic: str, text: str = "?") -> QPushButton:
    """
    Return a small '?' QPushButton pre-wired to open the help browser
    at *topic*.  Drop it anywhere in a module's toolbar / top bar.

    Example
    -------
    ::
        btn = make_help_button(self, "siam_run_list")
        top_bar.addWidget(btn)
    """
    btn = QPushButton(text)
    btn.setToolTip("Open help for this module (F1)")
    btn.setFixedWidth(28)
    btn.setStyleSheet("""
        QPushButton {
            font-weight: bold; color: #1a6ac4;
            border: 1px solid #b0c0d8; border-radius: 4px;
            background: #eff6ff; padding: 2px 6px;
        }
        QPushButton:hover   { background: #dbeafe; border-color: #3B82F6; }
        QPushButton:pressed { background: #bfdbfe; }
    """)
    btn.clicked.connect(lambda: show_help(parent, topic))
    return btn
