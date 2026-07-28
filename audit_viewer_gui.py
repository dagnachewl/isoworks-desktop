"""
audit_viewer_gui.py — Read-only audit trail viewer for IsoWorks.

Displays rows from audit.logged_actions with:
  • Filter bar  — table, operation, user, date range, free-text search
  • Results grid — timestamp, user, operation, table, changed fields
  • Diff panel  — OLD vs NEW values for the selected row, changed cells highlighted
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from PyQt5.QtCore import Qt, QDate, QSortFilterProxyModel
from PyQt5.QtGui import (
    QStandardItemModel, QStandardItem, QColor, QBrush, QFont
)
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QDateEdit, QLineEdit, QPushButton, QTableView, QHeaderView,
    QAbstractItemView, QSplitter, QTextEdit, QGroupBox, QSizePolicy,
    QFrame
)
from sqlalchemy import text

from db_core import db_manager
from gui_utils import show_message

log = logging.getLogger(__name__)

# Tables registered in migration 002 — shown even before any audit row exists
_AUDITED_TABLES = sorted([
    "public.Analysis",
    "public.Sample",
    "SIAM.SIAnalysisResult",
    "SIAM.SIAnalysisRun",
    "TRIMS.Electrolysis",
    "TRIMS.ElectrolysisRun",
    "TRIMS.LSCLoadList",
    "TRIMS.LSCResult",
    "TRIMS.LSCRun",
])

# ── palette ──────────────────────────────────────────────────────────────────
_COL_HDR        = "#37474F"   # header bar
_COL_INSERT     = "#E8F5E9"   # row bg for INSERT
_COL_UPDATE     = "#E3F2FD"   # row bg for UPDATE
_COL_DELETE     = "#FFEBEE"   # row bg for DELETE
_COL_CHANGED_BG = "#FFF9C4"   # diff panel — changed field background
_COL_CHANGED_FG = "#E65100"   # diff panel — changed field text

_OP_COLORS = {
    "INSERT": ("#1B5E20", _COL_INSERT),
    "UPDATE": ("#0D47A1", _COL_UPDATE),
    "DELETE": ("#B71C1C", _COL_DELETE),
}

_TABLE_STYLE = """
QTableView {
    gridline-color: #E0E0E0;
    font-size: 9pt;
    alternate-background-color: #F5F5F5;
}
QHeaderView::section {
    background: #ECEFF1;
    border: none;
    border-bottom: 1px solid #B0BEC5;
    padding: 4px 6px;
    font-weight: bold;
    font-size: 9pt;
}
"""


# ============================================================================
#  Audit viewer widget
# ============================================================================
class AuditViewerWidget(QWidget):

    _COLS = ["#", "Timestamp", "User", "Operation", "Table", "Changed Fields"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw_rows: list = []      # full result set for diff lookup

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        root.addWidget(self._build_filter_bar())
        root.addWidget(self._build_body(), 1)
        root.addWidget(self._build_status_bar())

        self._load_filter_combos()
        self.refresh()

    # ── header ────────────────────────────────────────────────────────────────
    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(44)
        w.setStyleSheet(f"background:{_COL_HDR};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(12, 4, 12, 4)
        title = QLabel("Audit Trail")
        title.setStyleSheet("color:white;font-size:14pt;font-weight:bold;")
        lay.addWidget(title)
        lay.addStretch()
        btn_export = QPushButton("⬇ Export CSV")
        btn_export.setStyleSheet(
            "QPushButton{color:white;background:transparent;border:1px solid white;"
            "border-radius:4px;padding:4px 10px;font-size:10pt;}"
            "QPushButton:hover{background:rgba(255,255,255,30);}"
        )
        btn_export.clicked.connect(self._export_csv)
        lay.addWidget(btn_export)
        return w

    # ── filter bar ────────────────────────────────────────────────────────────
    def _build_filter_bar(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet("background:#ECEFF1;border-bottom:1px solid #B0BEC5;")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(6)

        def lbl(t):
            l = QLabel(t)
            l.setStyleSheet("font-size:9pt;color:#546E7A;")
            return l

        self.cmbTable = QComboBox(); self.cmbTable.setMinimumWidth(180)
        self.cmbOperation = QComboBox()
        for op in ("All Operations", "INSERT", "UPDATE", "DELETE"):
            self.cmbOperation.addItem(op)
        self.cmbUser = QComboBox(); self.cmbUser.setMinimumWidth(120)

        self.dtFrom = QDateEdit()
        self.dtFrom.setCalendarPopup(True)
        self.dtFrom.setDate(QDate.currentDate().addDays(-30))
        self.dtFrom.setDisplayFormat("yyyy-MM-dd")

        self.dtTo = QDateEdit()
        self.dtTo.setCalendarPopup(True)
        self.dtTo.setDate(QDate.currentDate())
        self.dtTo.setDisplayFormat("yyyy-MM-dd")

        self.txtSearch = QLineEdit()
        self.txtSearch.setPlaceholderText("Search changed fields or values…")
        self.txtSearch.setMinimumWidth(200)
        self.txtSearch.returnPressed.connect(self.refresh)

        btn = QPushButton("Apply")
        btn.setStyleSheet(
            "QPushButton{background:#37474F;color:white;border-radius:3px;padding:4px 12px;}"
            "QPushButton:hover{background:#546E7A;}"
        )
        btn.clicked.connect(self.refresh)

        btn_reset = QPushButton("Reset")
        btn_reset.setStyleSheet(
            "QPushButton{background:transparent;color:#546E7A;border:1px solid #B0BEC5;"
            "border-radius:3px;padding:4px 8px;}"
            "QPushButton:hover{background:#CFD8DC;}"
        )
        btn_reset.clicked.connect(self._reset_filters)

        for w in [lbl("Table:"), self.cmbTable,
                  lbl("Operation:"), self.cmbOperation,
                  lbl("User:"), self.cmbUser,
                  lbl("From:"), self.dtFrom,
                  lbl("To:"), self.dtTo,
                  self.txtSearch, btn, btn_reset]:
            lay.addWidget(w)
        lay.addStretch()
        return bar

    # ── main body (splitter: grid + diff) ────────────────────────────────────
    def _build_body(self) -> QSplitter:
        splitter = QSplitter(Qt.Vertical)

        # — Results grid —
        self._model = QStandardItemModel(0, len(self._COLS))
        self._model.setHorizontalHeaderLabels(self._COLS)

        self._proxy = QSortFilterProxyModel()
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._proxy.setFilterKeyColumn(-1)  # all columns

        self._grid = QTableView()
        self._grid.setModel(self._proxy)
        self._grid.setStyleSheet(_TABLE_STYLE)
        self._grid.setAlternatingRowColors(True)
        self._grid.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self._grid.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._grid.verticalHeader().hide()
        self._grid.verticalHeader().setDefaultSectionSize(22)
        self._grid.horizontalHeader().setStretchLastSection(True)
        self._grid.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._grid.setColumnWidth(0, 60)   # #
        self._grid.setColumnWidth(1, 160)  # Timestamp
        self._grid.setColumnWidth(2, 100)  # User
        self._grid.setColumnWidth(3, 80)   # Operation
        self._grid.setColumnWidth(4, 200)  # Table
        self._grid.selectionModel().currentRowChanged.connect(self._on_row_selected)

        splitter.addWidget(self._grid)

        # — Diff panel —
        diff_box = QGroupBox("Change Detail")
        diff_lay = QVBoxLayout(diff_box)
        diff_lay.setContentsMargins(4, 4, 4, 4)
        self._diff_view = QTextEdit()
        self._diff_view.setReadOnly(True)
        self._diff_view.setStyleSheet("font-family:monospace;font-size:9pt;")
        diff_lay.addWidget(self._diff_view)
        splitter.addWidget(diff_box)

        splitter.setSizes([520, 240])
        return splitter

    # ── status bar ────────────────────────────────────────────────────────────
    def _build_status_bar(self) -> QLabel:
        self._status = QLabel("")
        self._status.setStyleSheet(
            "background:#ECEFF1;color:#546E7A;padding:2px 8px;font-size:9pt;"
        )
        return self._status

    # ── populate filter combos ────────────────────────────────────────────────
    def _load_filter_combos(self):
        # Start with the hardcoded set; extend with whatever the DB reports.
        table_set: set = set(_AUDITED_TABLES)
        user_list: list = []

        try:
            with db_manager.get_connection() as conn:
                for r in conn.execute(text(
                    "SELECT DISTINCT schema_name || '.' || table_name AS t "
                    "FROM audit.logged_actions ORDER BY t"
                )).fetchall():
                    table_set.add(r.t)

                user_list = [
                    r.u for r in conn.execute(text(
                        "SELECT DISTINCT COALESCE(app_user, db_user, '?') AS u "
                        "FROM audit.logged_actions WHERE app_user IS NOT NULL "
                        "   OR db_user IS NOT NULL ORDER BY u"
                    )).fetchall()
                    if r.u
                ]
        except Exception as exc:
            log.warning("Audit combo load: %s", exc)

        # Preserve the current selection across refresh
        prev_table = self.cmbTable.currentData()
        prev_user  = self.cmbUser.currentData()

        self.cmbTable.blockSignals(True)
        self.cmbTable.clear()
        self.cmbTable.addItem("All Tables", None)
        for t in sorted(table_set):
            self.cmbTable.addItem(t, t)
        idx = self.cmbTable.findData(prev_table)
        self.cmbTable.setCurrentIndex(max(0, idx))
        self.cmbTable.blockSignals(False)

        self.cmbUser.blockSignals(True)
        self.cmbUser.clear()
        self.cmbUser.addItem("All Users", None)
        for u in user_list:
            self.cmbUser.addItem(u, u)
        idx = self.cmbUser.findData(prev_user)
        self.cmbUser.setCurrentIndex(max(0, idx))
        self.cmbUser.blockSignals(False)

    # ── data fetch ────────────────────────────────────────────────────────────
    def refresh(self):
        clauses = [
            "changed_at::date >= :df",
            "changed_at::date <= :dt",
        ]
        params: dict = {
            "df": self.dtFrom.date().toPyDate(),
            "dt": self.dtTo.date().toPyDate(),
        }

        tbl = self.cmbTable.currentData()
        if tbl:
            clauses.append("schema_name || '.' || table_name = :tbl")
            params["tbl"] = tbl

        op = self.cmbOperation.currentText()
        if op != "All Operations":
            clauses.append("operation = :op")
            params["op"] = op

        user = self.cmbUser.currentData()
        if user:
            clauses.append("COALESCE(app_user, db_user) = :usr")
            params["usr"] = user

        search = self.txtSearch.text().strip()
        if search:
            clauses.append(
                "(array_to_string(changed_fields, ' ') ILIKE :q "
                " OR old_data::text ILIKE :q "
                " OR new_data::text ILIKE :q)"
            )
            params["q"] = f"%{search}%"

        sql = text(f"""
            SELECT action_id, changed_at, COALESCE(app_user, db_user, '?') AS app_user,
                   operation, schema_name, table_name,
                   COALESCE(
                       array_to_string(changed_fields, ', '),
                       CASE operation WHEN 'INSERT' THEN '(new row)' ELSE '(deleted)' END
                   ) AS summary,
                   old_data, new_data, changed_fields
            FROM audit.logged_actions
            WHERE {' AND '.join(clauses)}
            ORDER BY changed_at DESC
            LIMIT 2000
        """)

        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            log.error("Audit fetch: %s", exc)
            show_message(self, "Audit Error", str(exc))
            return

        self._raw_rows = rows
        self._populate_grid(rows)
        self._diff_view.clear()
        self._load_filter_combos()

    def _populate_grid(self, rows):
        self._model.setRowCount(0)
        for r in rows:
            op = r.operation
            _, bg = _OP_COLORS.get(op, ("#000", "#FFF"))
            bg_brush = QBrush(QColor(bg))

            ts = r.changed_at.strftime("%Y-%m-%d %H:%M:%S") if r.changed_at else ""
            cells = [
                str(r.action_id),
                ts,
                r.app_user or "",
                op,
                f"{r.schema_name}.{r.table_name}",
                r.summary or "",
            ]
            items = []
            for i, val in enumerate(cells):
                item = QStandardItem(val)
                item.setBackground(bg_brush)
                if i == 3:  # Operation column — coloured text
                    fg, _ = _OP_COLORS.get(op, ("#000", "#FFF"))
                    item.setForeground(QBrush(QColor(fg)))
                    item.setFont(QFont("Arial", 9, QFont.Bold))
                items.append(item)
            self._model.appendRow(items)

        n = len(rows)
        self._status.setText(
            f"{n} record{'s' if n != 1 else ''} "
            f"{'(limit 2 000 — narrow your filter)' if n == 2000 else ''}"
        )

    # ── row selection → diff panel ────────────────────────────────────────────
    def _on_row_selected(self, current, _previous):
        src_row = self._proxy.mapToSource(current).row()
        if src_row < 0 or src_row >= len(self._raw_rows):
            self._diff_view.clear()
            return
        self._render_diff(self._raw_rows[src_row])

    def _render_diff(self, row):
        op        = row.operation
        old_data  = row.old_data   # already a dict via psycopg2 JSONB
        new_data  = row.new_data
        changed   = set(row.changed_fields or [])

        # Normalise — psycopg2 may return dict or str
        def _to_dict(v):
            if v is None:
                return {}
            if isinstance(v, dict):
                return v
            try:
                return json.loads(v)
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); return {"raw": str(v)}

        old_dict = _to_dict(old_data)
        new_dict = _to_dict(new_data)
        all_keys = sorted(set(old_dict) | set(new_dict))

        ts  = row.changed_at.strftime("%Y-%m-%d %H:%M:%S") if row.changed_at else ""
        tbl = f"{row.schema_name}.{row.table_name}"

        html_parts = [f"""
        <style>
          body {{font-family:monospace;font-size:9pt;}}
          table {{border-collapse:collapse;width:100%;}}
          th {{background:#ECEFF1;padding:4px 8px;text-align:left;
               border-bottom:2px solid #B0BEC5;}}
          td {{padding:3px 8px;border-bottom:1px solid #EEEEEE;
               vertical-align:top;white-space:pre-wrap;word-break:break-all;}}
          .changed td {{background:{_COL_CHANGED_BG};color:{_COL_CHANGED_FG};font-weight:bold;}}
          .op-insert {{color:#1B5E20;font-weight:bold;}}
          .op-update {{color:#0D47A1;font-weight:bold;}}
          .op-delete {{color:#B71C1C;font-weight:bold;}}
        </style>
        <p style="margin:4px 0;">
          <span class="op-{op.lower()}">{op}</span>
          &nbsp;on&nbsp;<b>{tbl}</b>
          &nbsp;at&nbsp;{ts}
          &nbsp;by&nbsp;<b>{row.app_user or '?'}</b>
        </p>
        <table>
          <tr><th>Field</th><th>OLD value</th><th>NEW value</th></tr>
        """]

        for key in all_keys:
            old_val = old_dict.get(key)
            new_val = new_dict.get(key)
            cls     = ' class="changed"' if key in changed else ""
            old_str = "" if old_val is None else str(old_val)
            new_str = "" if new_val is None else str(new_val)
            # For INSERT show only new; for DELETE show only old
            if op == "INSERT":
                old_str = "<em style='color:#9E9E9E;'>—</em>"
            elif op == "DELETE":
                new_str = "<em style='color:#9E9E9E;'>—</em>"
            html_parts.append(
                f"<tr{cls}><td>{key}</td><td>{old_str}</td><td>{new_str}</td></tr>"
            )

        html_parts.append("</table>")
        self._diff_view.setHtml("".join(html_parts))

    # ── filter reset ──────────────────────────────────────────────────────────
    def _reset_filters(self):
        self.cmbTable.setCurrentIndex(0)
        self.cmbOperation.setCurrentIndex(0)
        self.cmbUser.setCurrentIndex(0)
        self.dtFrom.setDate(QDate.currentDate().addDays(-30))
        self.dtTo.setDate(QDate.currentDate())
        self.txtSearch.clear()
        self.refresh()

    # ── CSV export ────────────────────────────────────────────────────────────
    def _export_csv(self):
        from PyQt5.QtWidgets import QFileDialog
        import csv, os

        if not self._raw_rows:
            show_message(self, "Export", "No data to export.")
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Audit Log",
            f"AuditLog_{ts}.csv",
            "CSV Files (*.csv);;All Files (*)"
        )
        if not path:
            return
        headers = ["action_id", "changed_at", "app_user", "operation",
                   "table", "changed_fields", "old_data", "new_data"]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(headers)
                for r in self._raw_rows:
                    w.writerow([
                        r.action_id,
                        r.changed_at,
                        r.app_user,
                        r.operation,
                        f"{r.schema_name}.{r.table_name}",
                        ", ".join(r.changed_fields or []),
                        json.dumps(r.old_data) if r.old_data else "",
                        json.dumps(r.new_data) if r.new_data else "",
                    ])
            self._status.setText(f"Exported {len(self._raw_rows)} rows → {path}")
        except Exception as exc:
            show_message(self, "Export Error", str(exc))
