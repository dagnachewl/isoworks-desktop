"""
workflow_management_gui.py — Workflow management panel for IsoWorks.
Provides WorkflowManagementWidget with CRUD operations on the Workflow table.
The Jobs sub-panel is rendered as a graphical pipeline: each WorkflowJob appears
as a clickable card connected by arrows in RunSequence order.
"""
import sys
import logging
import getpass
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QComboBox, QLineEdit,
    QPushButton, QCheckBox, QLabel, QFormLayout,
    QMessageBox, QSplitter, QTextEdit, QToolButton,
    QDialog, QGraphicsView, QGraphicsScene, QGraphicsObject,
    QGraphicsPathItem, QGraphicsItem, QMenu, QAction
)
from PyQt5.QtGui import (
    QPainter, QPainterPath, QColor, QPen, QBrush, QFont
)
from PyQt5.QtCore import Qt, QRectF, QPointF, pyqtSignal

from db_core import db_manager
from sqlalchemy import text
from help_browser import make_help_button

try:
    from workflow_job_editor_dialog_gui import WorkflowJobEditorDialog
except ImportError:
    WorkflowJobEditorDialog = None
    logging.warning("workflow_job_editor_dialog_gui.py not found — job editing disabled.")

# ── Pipeline visual constants ──────────────────────────────────────────────────
_NW,  _NH  = 180, 100  # node width, height
_GAP       = 52         # horizontal gap between nodes (arrow space)
_PAD_X     = 24         # left/right scene margin
_PAD_Y     = 16         # top/bottom scene margin

_C_BG_DEFAULT  = QColor("#FFFFFF")
_C_BG_PREREQ   = QColor("#EBF5FB")   # light blue  — prerequisite step
_C_BG_REPORT   = QColor("#EAFAF1")   # light green — reporting/final step
_C_BG_OBSOLETE = QColor("#F2F3F4")   # light gray  — obsolete
_C_BORDER      = QColor("#AEB6BF")
_C_BORDER_SEL  = QColor("#1A5276")
_C_BADGE_PRE   = QColor("#1A5276")
_C_BADGE_REP   = QColor("#1E8449")
_C_BADGE_OBS   = QColor("#7F8C8D")
_C_SEQ         = QColor("#5D6D7E")
_C_ARROW       = QColor("#7F8C8D")
_C_SCENE_BG    = QColor("#F4F6F7")


# ── Job node ──────────────────────────────────────────────────────────────────

class _JobNode(QGraphicsObject):
    """Clickable rounded-rect card representing one WorkflowJob."""

    clicked       = pyqtSignal(int)   # emits workflowjobid
    delete_req    = pyqtSignal(int)   # emits workflowjobid (from context menu)

    def __init__(self, job: dict, col_idx: int) -> None:
        super().__init__()
        self._job = job
        self.setPos(_PAD_X + col_idx * (_NW + _GAP), _PAD_Y)
        self.setFlag(QGraphicsItem.ItemIsSelectable)
        self.setCursor(Qt.PointingHandCursor)
        self.setAcceptHoverEvents(True)
        self._hovered = False
        self.setToolTip(
            f"Seq {job['seq']}: {job['name']}\n"
            f"Procedure: {job['proc'] or '—'}\n"
            "Click to edit · Right-click for more options"
        )

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, _NW, _NH)

    def _bg(self) -> QColor:
        if self._job["obsolete"]: return _C_BG_OBSOLETE
        if self._job["report"]:   return _C_BG_REPORT
        if self._job["prereq"]:   return _C_BG_PREREQ
        return _C_BG_DEFAULT

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0, 0, _NW, _NH)

        # ── shadow (offset rect, drawn first) ─────────────────────────────
        shadow_path = QPainterPath()
        shadow_path.addRoundedRect(r.translated(2, 3), 10, 10)
        painter.fillPath(shadow_path, QColor(0, 0, 0, 18))

        # ── card body ─────────────────────────────────────────────────────
        card_path = QPainterPath()
        card_path.addRoundedRect(r, 10, 10)
        painter.fillPath(card_path, QBrush(self._bg()))

        border_col = _C_BORDER_SEL if (self.isSelected() or self._hovered) else _C_BORDER
        border_w   = 2.0           if (self.isSelected() or self._hovered) else 1.2
        painter.strokePath(card_path, QPen(border_col, border_w))

        # ── sequence badge (circle, top-left) ─────────────────────────────
        bx, by, br = 10, 10, 22
        painter.setBrush(QBrush(_C_SEQ))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(bx, by, br, br)
        painter.setPen(QPen(Qt.white))
        sf = QFont(); sf.setPointSize(8); sf.setBold(True)
        painter.setFont(sf)
        painter.drawText(QRectF(bx, by, br, br), Qt.AlignCenter, str(self._job["seq"]))

        # ── job name ──────────────────────────────────────────────────────
        painter.setPen(QPen(QColor("#1C2833") if not self._job["obsolete"] else QColor("#7F8C8D")))
        nf = QFont(); nf.setPointSize(11); nf.setBold(True)
        if self._job["obsolete"]: nf.setItalic(True)
        painter.setFont(nf)
        painter.drawText(
            QRectF(8, 30, _NW - 16, 30),
            Qt.AlignCenter | Qt.TextWordWrap,
            self._job["name"] or "—"
        )

        # ── procedure (subtitle) ──────────────────────────────────────────
        proc = (self._job["proc"] or "").strip()
        if proc:
            painter.setPen(QPen(QColor("#808B96")))
            pf = QFont(); pf.setPointSize(8)
            painter.setFont(pf)
            painter.drawText(
                QRectF(8, 60, _NW - 16, 18),
                Qt.AlignCenter | Qt.TextWordWrap,
                proc
            )

        # ── badge strip (bottom) ──────────────────────────────────────────
        badges, badge_col = [], _C_BADGE_PRE
        if self._job["prereq"]:   badges.append("PRE-REQ");   badge_col = _C_BADGE_PRE
        if self._job["report"]:   badges.append("REPORTING"); badge_col = _C_BADGE_REP
        if self._job["obsolete"]: badges.append("OBSOLETE");  badge_col = _C_BADGE_OBS
        if badges:
            bp = QPainterPath()
            bp.addRoundedRect(QRectF(10, _NH - 19, _NW - 20, 14), 3, 3)
            painter.fillPath(bp, badge_col)
            painter.setPen(QPen(Qt.white))
            bf = QFont(); bf.setPointSize(7); bf.setBold(True)
            painter.setFont(bf)
            painter.drawText(
                QRectF(10, _NH - 19, _NW - 20, 14),
                Qt.AlignCenter,
                " · ".join(badges)
            )

    def hoverEnterEvent(self, event):
        self._hovered = True;  self.update()

    def hoverLeaveEvent(self, event):
        self._hovered = False; self.update()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._job["jid"])

    def contextMenuEvent(self, event):
        menu = QMenu()
        act_edit   = menu.addAction("Edit…")
        menu.addSeparator()
        act_delete = menu.addAction("Delete")
        act_delete.setEnabled(not self._job["obsolete"])
        chosen = menu.exec_(event.screenPos())
        if chosen == act_edit:
            self.clicked.emit(self._job["jid"])
        elif chosen == act_delete:
            self.delete_req.emit(self._job["jid"])


# ── Arrow helper ──────────────────────────────────────────────────────────────

def _arrow_item(x1: float, y: float, x2: float) -> QGraphicsPathItem:
    """Horizontal arrow from (x1, y) → (x2, y) with a filled triangle head."""
    path = QPainterPath()
    path.moveTo(x1, y)
    path.lineTo(x2 - 11, y)          # shaft
    path.moveTo(x2, y)                # arrowhead
    path.lineTo(x2 - 11, y - 6)
    path.lineTo(x2 - 11, y + 6)
    path.closeSubpath()
    item = QGraphicsPathItem(path)
    item.setPen(QPen(_C_ARROW, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    item.setBrush(QBrush(_C_ARROW))
    item.setZValue(-1)
    return item


# ── Pipeline view ─────────────────────────────────────────────────────────────

class WorkflowPipelineView(QGraphicsView):
    """
    Horizontal pipeline diagram for a single workflow's jobs.
    Emits job_edit_requested(jid) when a node is clicked.
    Emits job_delete_requested(jid) when delete is chosen from a node's context menu.
    """
    job_edit_requested   = pyqtSignal(int)
    job_delete_requested = pyqtSignal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setFixedHeight(_NH + _PAD_Y * 2 + 22)
        self.setStyleSheet(
            "QGraphicsView { background:#F4F6F7; border:1px solid #D5D8DC; border-radius:6px; }"
        )

    def load(self, workflow_id) -> None:
        self._scene.clear()
        if not workflow_id:
            self._empty_message("Select a workflow to view its pipeline.")
            return

        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT wj.WorkflowJobID,
                           wj.RunSequence,
                           wj.JobName,
                           ap.ProcedureName,
                           wj.IsPreRequisite,
                           wj.IsObsolete
                    FROM   WorkflowJob wj
                    LEFT JOIN AnalysisProcedure ap
                           ON ap.ProcedureID = wj.ProcedureID
                    WHERE  wj.WorkflowID = :wid
                    ORDER  BY wj.RunSequence
                """), {"wid": workflow_id}).fetchall()
        except Exception as exc:
            logging.error("Pipeline load failed: %s", exc)
            self._empty_message("Failed to load jobs.")
            return

        if not rows:
            self._empty_message("No jobs yet.  Click  Add Job  to get started.")
            return

        nodes: list[_JobNode] = []
        for idx, row in enumerate(rows):
            # IsReportingJob may not exist in all schemas; treat as False if absent
            try:   is_report = bool(row.IsReportingJob)
            except Exception: is_report = False

            job = dict(
                jid      = row.WorkflowJobID,
                seq      = row.RunSequence,
                name     = row.JobName or "",
                proc     = row.ProcedureName or "",
                prereq   = bool(row.IsPreRequisite),
                report   = is_report,
                obsolete = bool(row.IsObsolete),
            )
            node = _JobNode(job, idx)
            node.clicked.connect(self.job_edit_requested)
            node.delete_req.connect(self.job_delete_requested)
            self._scene.addItem(node)
            nodes.append(node)

        # Arrows between consecutive nodes
        for i in range(len(nodes) - 1):
            n = nodes[i]
            x1 = n.x() + _NW
            x2 = nodes[i + 1].x()
            y  = n.y() + _NH / 2
            self._scene.addItem(_arrow_item(x1, y, x2))

        total_w = _PAD_X * 2 + len(nodes) * _NW + max(0, len(nodes) - 1) * _GAP
        total_h = _NH + _PAD_Y * 2
        self._scene.setSceneRect(0, 0, total_w, total_h)

    def _empty_message(self, text_: str) -> None:
        self._scene.setSceneRect(0, 0, 400, _NH + _PAD_Y * 2)
        ti = self._scene.addText(text_)
        ti.setDefaultTextColor(QColor("#AAB7B8"))
        tf = QFont(); tf.setItalic(True); tf.setPointSize(9)
        ti.setFont(tf)
        br = ti.boundingRect()
        ti.setPos((400 - br.width()) / 2, (_NH + _PAD_Y * 2 - br.height()) / 2)


# ── Main widget ───────────────────────────────────────────────────────────────

class WorkflowManagementWidget(QWidget):
    """Workflow CRUD panel with an embedded pipeline diagram for jobs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_workflow_id = None
        self.current_media_id    = None
        self.is_new_record       = False

        self.main_layout = QVBoxLayout(self)
        self.main_layout.addLayout(self._create_action_buttons())
        self.main_layout.addWidget(self._create_filter_group())
        self.main_layout.addWidget(self._create_selection_group())
        self.main_layout.addSpacing(10)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._create_details_group())
        splitter.addWidget(self._create_pipeline_group())
        splitter.setSizes([420, 180])
        self.main_layout.addWidget(splitter, 1)

        self._connect_signals()

        try:
            db_manager.get_engine()
            self.load_category_combo()
        except Exception as exc:
            logging.error("WorkflowManagementWidget init: %s", exc)

        self._set_edit_mode(False)

    # ── UI builders ───────────────────────────────────────────────────────────

    def _create_action_buttons(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        self.btnNew    = QPushButton("New")
        self.btnEdit   = QPushButton("Edit")
        self.btnSave   = QPushButton("Save")
        self.btnCancel = QPushButton("Cancel")
        self.btnDelete = QPushButton("Delete")
        bar.addStretch(1)
        for b in [self.btnNew, self.btnEdit, self.btnSave, self.btnCancel, self.btnDelete]:
            bar.addWidget(b)
        bar.addWidget(make_help_button(self, "workflow_mgmt"))
        return bar

    def _create_filter_group(self) -> QGroupBox:
        g = QGroupBox("Filter")
        f = QFormLayout(g)
        self.cmbCategoryID = QComboBox()
        self.cmbMedia      = QComboBox()
        self.media_label   = QLabel("Media:")
        f.addRow("Category:", self.cmbCategoryID)
        f.addRow(self.media_label, self.cmbMedia)
        return g

    def _create_selection_group(self) -> QGroupBox:
        g = QGroupBox("Selection")
        l = QHBoxLayout(g)
        l.addWidget(QLabel("Select:"))
        self.cmbSelectionList = QComboBox()
        l.addWidget(self.cmbSelectionList, 1)
        self.btnMoveBack    = QToolButton(); self.btnMoveBack.setText("<")
        self.btnMoveForward = QToolButton(); self.btnMoveForward.setText(">")
        l.addWidget(self.btnMoveBack)
        l.addWidget(self.btnMoveForward)
        return g

    def _create_details_group(self) -> QGroupBox:
        g = QGroupBox("Details")
        l = QGridLayout(g)

        f1 = QFormLayout()
        self.txtWorkflowID   = QLineEdit(); self.txtWorkflowID.setReadOnly(True)
        self.txtWorkflowName = QLineEdit()
        self.txtAbbreviation = QLineEdit()
        self.txtPrice        = QLineEdit()
        self.chkIsObsolete   = QCheckBox("Obsolete")
        self.lblObsoleteWarning = QLabel("OBSOLETE")
        self.lblObsoleteWarning.setStyleSheet("color:red;font-weight:bold")
        self.txtComments = QTextEdit(); self.txtComments.setMaximumHeight(80)
        f1.addRow("ID:",   self.txtWorkflowID)
        f1.addRow("Name:", self.txtWorkflowName)
        f1.addRow("Abbr:", self.txtAbbreviation)
        f1.addRow("Price:", self.txtPrice)
        f1.addRow(self.chkIsObsolete)
        f1.addRow(self.lblObsoleteWarning)
        f1.addRow("Rem:", self.txtComments)

        f2 = QFormLayout()
        self.txtReportingHeaderMemo = QTextEdit()
        self.txtReportingFooterMemo = QTextEdit()
        f2.addRow("Header:", self.txtReportingHeaderMemo)
        f2.addRow("Footer:", self.txtReportingFooterMemo)

        l.addLayout(f1, 0, 0)
        l.addLayout(f2, 0, 1)
        return g

    def _create_pipeline_group(self) -> QGroupBox:
        g = QGroupBox("Jobs — Pipeline View")
        v = QVBoxLayout(g)

        # Toolbar
        bar = QHBoxLayout()
        self.btnAddJob = QPushButton("Add Job")
        self.btnAddJob.setStyleSheet("""
            QPushButton { background:#27ae60; color:white; font-weight:bold;
                          border:none; padding:4px 14px; border-radius:4px; }
            QPushButton:hover    { background:#219a52; }
            QPushButton:disabled { background:#a9dfbf; color:#7f8c8d; }
        """)
        bar.addStretch()
        bar.addWidget(self.btnAddJob)
        v.addLayout(bar)

        # Pipeline view
        self.pipeline_view = WorkflowPipelineView(self)
        self.pipeline_view.job_edit_requested.connect(self._on_node_edit)
        self.pipeline_view.job_delete_requested.connect(self._on_node_delete)
        v.addWidget(self.pipeline_view)
        return g

    def _connect_signals(self):
        self.cmbCategoryID.currentIndexChanged.connect(self.on_cat)
        self.cmbMedia.currentIndexChanged.connect(self.on_med)
        self.cmbSelectionList.currentIndexChanged.connect(self.on_sel)
        self.btnNew.clicked.connect(self.on_new_clicked)
        self.btnEdit.clicked.connect(self.on_edit_clicked)
        self.btnSave.clicked.connect(self.on_save_clicked)
        self.btnCancel.clicked.connect(self.on_cancel)
        self.btnDelete.clicked.connect(self.on_delete_clicked)
        self.btnAddJob.clicked.connect(self._on_add_job)
        self.btnMoveBack.clicked.connect(self.on_move_back_clicked)
        self.btnMoveForward.clicked.connect(self.on_move_forward_clicked)
        self.chkIsObsolete.toggled.connect(
            lambda c: self.lblObsoleteWarning.setVisible(c)
        )

    # ── Edit-mode gating ──────────────────────────────────────────────────────

    def _set_edit_mode(self, edit: bool):
        browse = not edit
        has_id = self.current_workflow_id is not None

        self.cmbCategoryID.setEnabled(browse)
        self.cmbMedia.setEnabled(browse)
        self.cmbSelectionList.setEnabled(browse)
        self.btnMoveBack.setEnabled(browse)
        self.btnMoveForward.setEnabled(browse)

        self.btnNew.setEnabled(browse)
        self.btnEdit.setEnabled(browse and has_id)
        self.btnDelete.setEnabled(browse and has_id)
        self.btnSave.setEnabled(edit)
        self.btnCancel.setEnabled(edit)
        self.btnAddJob.setEnabled(browse and has_id)

        for w in [self.txtWorkflowName, self.txtAbbreviation, self.txtPrice,
                  self.txtComments, self.txtReportingHeaderMemo, self.txtReportingFooterMemo]:
            w.setReadOnly(browse)
        self.chkIsObsolete.setEnabled(edit)

    # ── Data helpers ──────────────────────────────────────────────────────────

    def load_category_combo(self):
        try:
            with db_manager.get_connection() as conn:
                sql = f"""SELECT ID, {db_manager.sql_concat("ID", "': '", "sName")} FROM Job_Procedure WHERE ID In (3,4,5,11,14) ORDER BY ID"""
                self.cmbCategoryID.blockSignals(True)
                self.cmbCategoryID.clear()
                for r in conn.execute(text(sql)):
                    self.cmbCategoryID.addItem(r[1], r[0])
                idx = self.cmbCategoryID.findData(5)
                self.cmbCategoryID.setCurrentIndex(idx if idx > -1 else 0)
        except Exception as exc:
            logging.error("load_category_combo: %s", exc)
        finally:
            self.cmbCategoryID.blockSignals(False)
            self.on_cat()

    def on_cat(self):
        cid = self.cmbCategoryID.currentData()
        self.current_media_id = 1
        if not cid: return
        if cid in [1, 2, 3]:     self.current_media_id = 200
        elif cid in [12, 13, 14]: self.current_media_id = 300
        elif cid in [10, 11]:     self.current_media_id = 202
        self.load_selection_list(self.current_media_id)
        show_media = (cid == 5)
        self.cmbMedia.setVisible(show_media)
        self.media_label.setVisible(show_media)
        if show_media: self.load_media_combo()

    def load_media_combo(self):
        try:
            with db_manager.get_connection() as conn:
                sql = f"""SELECT MediaID, {db_manager.sql_concat("Prefix", "' : '", "medianame")} FROM Media WHERE MediaID=1 OR MediaID=58 ORDER BY Prefix DESC, MediaID"""
                self.cmbMedia.blockSignals(True)
                self.cmbMedia.clear()
                for r in conn.execute(text(sql)):
                    self.cmbMedia.addItem(r[1], r[0])
                self.cmbMedia.setCurrentIndex(0)
        except Exception as exc:
            logging.error("load_media_combo: %s", exc)
        finally:
            self.cmbMedia.blockSignals(False)

    def on_med(self):
        self.current_media_id = self.cmbMedia.currentData()
        if self.current_media_id:
            self.load_selection_list(self.current_media_id)

    def load_selection_list(self, mid):
        self.cmbSelectionList.blockSignals(True)
        self.cmbSelectionList.clear()
        try:
            with db_manager.get_connection() as conn:
                sql = f"""SELECT WorkflowID, {db_manager.sql_concat("WorkflowID", "'---'", "WorkflowName")} FROM Workflow WHERE MediaID = :mid ORDER BY WorkflowID"""
                for r in conn.execute(text(sql), {"mid": mid}):
                    self.cmbSelectionList.addItem(r[1], r[0])
        except Exception as exc:
            logging.error("load_selection_list: %s", exc)
        finally:
            self.cmbSelectionList.blockSignals(False)
            self.cmbSelectionList.setCurrentIndex(0)
            self.on_sel()

    def on_sel(self):
        self.populate_form(self.cmbSelectionList.currentData())

    def populate_form(self, wid):
        if not wid:
            self.current_workflow_id = None
            self._clear_form()
            self.pipeline_view.load(None)
            self._set_edit_mode(False)
            return
        self.current_workflow_id = wid
        try:
            with db_manager.get_connection() as conn:
                r = conn.execute(
                    text("SELECT * FROM Workflow WHERE WorkflowID = :wid"), {"wid": wid}
                ).fetchone()
                if r:
                    self.txtWorkflowID.setText(str(r.WorkflowID))
                    self.txtWorkflowName.setText(r.WorkflowName or "")
                    self.txtAbbreviation.setText(r.Abbreviation or "")
                    self.txtPrice.setText(str(r.Price) if r.Price is not None else "")
                    self.chkIsObsolete.setChecked(bool(r.IsObsolete))
                    self.lblObsoleteWarning.setVisible(bool(r.IsObsolete))
                    self.txtComments.setText(r.Comments or "")
                    self.txtReportingHeaderMemo.setText(r.ReportingHeaderMemo or "")
                    self.txtReportingFooterMemo.setText(r.ReportingFooterMemo or "")
        except Exception as exc:
            logging.error("populate_form: %s", exc)
        self.pipeline_view.load(wid)
        self._set_edit_mode(False)

    def _clear_form(self):
        for w in [self.txtWorkflowID, self.txtWorkflowName, self.txtAbbreviation,
                  self.txtPrice, self.txtComments, self.txtReportingHeaderMemo,
                  self.txtReportingFooterMemo]:
            w.clear()
        self.chkIsObsolete.setChecked(False)

    # ── Workflow CRUD ─────────────────────────────────────────────────────────

    def on_cancel(self):
        self._set_edit_mode(False)
        self.populate_form(self.current_workflow_id)

    def on_new_clicked(self):
        self.is_new_record = True
        self.current_workflow_id = None
        self._clear_form()
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT MAX(WorkflowID) FROM Workflow")).fetchone()
                self.txtWorkflowID.setText(str((res[0] or 10000) + 1))
        except Exception as exc:
            logging.error("on_new_clicked: %s", exc)
        self._set_edit_mode(True)
        self.txtWorkflowName.setFocus()

    def on_edit_clicked(self):
        if not self.current_workflow_id: return
        self.is_new_record = False
        self._set_edit_mode(True)

    def on_save_clicked(self):
        if not self.txtWorkflowName.text(): return
        try:
            with db_manager.get_connection() as conn:
                p = {
                    "nam":  self.txtWorkflowName.text(),
                    "abb":  self.txtAbbreviation.text(),
                    "mid":  self.current_media_id,
                    "com":  self.txtComments.toPlainText(),
                    "prc":  _to_float(self.txtPrice),
                    "head": self.txtReportingHeaderMemo.toPlainText(),
                    "foot": self.txtReportingFooterMemo.toPlainText(),
                    "obs":  self.chkIsObsolete.isChecked(),
                    "now":  datetime.now(),
                    "usr":  getpass.getuser(),
                }
                if self.is_new_record:
                    res = conn.execute(text("SELECT MAX(WorkflowID) FROM Workflow")).fetchone()
                    wid = (res[0] or 10000) + 1
                    p["wid"] = wid
                    self.current_workflow_id = wid
                    conn.execute(text("""
                        INSERT INTO Workflow
                            (WorkflowName, Abbreviation, MediaID, Comments, Price,
                             ReportingHeaderMemo, ReportingFooterMemo, IsObsolete,
                             ModifDateStamp, ModifUserStamp, CreateDateStamp,
                             CreateUserStamp, WorkflowID)
                        VALUES
                            (:nam, :abb, :mid, :com, :prc, :head, :foot, :obs,
                             :now, :usr, :now, :usr, :wid)
                    """), p)
                else:
                    p["wid"] = self.current_workflow_id
                    conn.execute(text("""
                        UPDATE Workflow
                        SET WorkflowName=:nam, Abbreviation=:abb, MediaID=:mid,
                            Comments=:com, Price=:prc, ReportingHeaderMemo=:head,
                            ReportingFooterMemo=:foot, IsObsolete=:obs,
                            ModifDateStamp=:now, ModifUserStamp=:usr
                        WHERE WorkflowID=:wid
                    """), p)
                conn.commit()
            QMessageBox.information(self, "Saved", "Workflow saved.")
            self.load_selection_list(self.current_media_id)
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))

    def on_delete_clicked(self):
        if QMessageBox.question(
            self, "Delete", "Delete this workflow and all its jobs?",
            QMessageBox.Yes | QMessageBox.No
        ) == QMessageBox.No:
            return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("DELETE FROM WorkflowJob WHERE WorkflowID=:wid"),
                             {"wid": self.current_workflow_id})
                conn.execute(text("DELETE FROM Workflow WHERE WorkflowID=:wid"),
                             {"wid": self.current_workflow_id})
                conn.commit()
            self.load_selection_list(self.current_media_id)
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))

    # ── Job pipeline actions ──────────────────────────────────────────────────

    def _on_add_job(self):
        if not WorkflowJobEditorDialog:
            QMessageBox.critical(self, "Error", "Job editor module not available.")
            return
        dlg = WorkflowJobEditorDialog(self.current_workflow_id, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            self.pipeline_view.load(self.current_workflow_id)

    def _on_node_edit(self, job_id: int):
        if not WorkflowJobEditorDialog:
            QMessageBox.critical(self, "Error", "Job editor module not available.")
            return
        dlg = WorkflowJobEditorDialog(self.current_workflow_id, job_id, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            self.pipeline_view.load(self.current_workflow_id)

    def _on_node_delete(self, job_id: int):
        if QMessageBox.question(
            self, "Delete Job", "Permanently delete this job?",
            QMessageBox.Yes | QMessageBox.No
        ) == QMessageBox.No:
            return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("DELETE FROM WorkflowJob WHERE WorkflowJobID = :jid"),
                             {"jid": job_id})
                conn.commit()
            self.pipeline_view.load(self.current_workflow_id)
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))

    # ── Navigation ────────────────────────────────────────────────────────────

    def on_move_back_clicked(self):
        i = self.cmbSelectionList.currentIndex()
        if i > 0: self.cmbSelectionList.setCurrentIndex(i - 1)

    def on_move_forward_clicked(self):
        i = self.cmbSelectionList.currentIndex()
        if i < self.cmbSelectionList.count() - 1:
            self.cmbSelectionList.setCurrentIndex(i + 1)


# ── Utilities ─────────────────────────────────────────────────────────────────

def _to_float(widget) -> float | None:
    try:    return float(widget.text())
    except: return None


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = WorkflowManagementWidget()
    w.show()
    sys.exit(app.exec_())
