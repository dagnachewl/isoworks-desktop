"""
tray_print_renderer.py — Shared physical tray-grid PDF renderer for IsoWorks.

Provides:
  draw_tray_cell(painter, ...)          — draw one vial cell in local coords
  render_tray_loadlist(printer, ...)    — render full tray grid onto QPrinter
  default_tray_cfg(specs, cols=8)       — derive grid dims from spec data
"""
import logging

from PyQt5.QtCore import Qt, QRectF, QPointF, QSettings as _AppSettings
from PyQt5.QtGui import QFont, QColor, QPen, QPainter
from PyQt5.QtPrintSupport import QPrinter

from sample_label_gui import _make_qr_pixmap


# ── Cell renderer ─────────────────────────────────────────────────────────────

def draw_tray_cell(painter, cell_w_px, cell_h_px, cell_w_mm, cell_h_mm,
                   vial_label, spec, show_qr, sample_type=0):
    """Draw one tray cell in local coords (0,0)→(cell_w_px, cell_h_px).
    sample_type != 0 (standard/blank/spike) renders SID/AID text in bold."""
    m = max(1.5, min(cell_w_px, cell_h_px) * 0.06)

    if spec:
        painter.fillRect(QRectF(0, 0, cell_w_px, cell_h_px), QColor("#FFFFFF"))
    # empty cells: no fill — paper background shows through (saves ink)

    if not spec:
        painter.setPen(QColor("#B0BEC5"))
        painter.setFont(QFont("Arial", max(5, int(min(cell_w_mm, cell_h_mm) * 0.22))))
        painter.drawText(QRectF(0, 0, cell_w_px, cell_h_px), Qt.AlignCenter, vial_label)
        return

    data    = dict(spec.lines)
    sid     = data.get("Sample ID", "")
    aid     = data.get("Analysis ID", "")
    qr_text = spec.qr_text

    portrait = cell_h_px > cell_w_px * 1.30
    use_qr   = show_qr and bool(qr_text) and cell_w_mm >= 12.0

    if use_qr and portrait:
        qr_sz = int(min(cell_w_px - 2 * m, cell_h_px * 0.52))
        qr_pm = _make_qr_pixmap(qr_text, qr_sz)
        if qr_pm:
            painter.drawPixmap(int((cell_w_px - qr_sz) / 2), int(m), qr_pm)
        txt_y = m + qr_sz + m
        txt_h = cell_h_px - txt_y - m
        row_h = txt_h / 3.0
        align = Qt.AlignHCenter | Qt.AlignVCenter
        pt_v  = max(7, int(cell_w_mm * 0.65))
        pt_s  = max(6, int(cell_w_mm * 0.45))

    elif use_qr:
        qr_sz = int(cell_h_px * 0.84)
        qr_pm = _make_qr_pixmap(qr_text, qr_sz)
        txt_x = m
        if qr_pm:
            painter.drawPixmap(int(m), int((cell_h_px - qr_sz) / 2), qr_pm)
            txt_x = m + qr_sz + m
        txt_y = m;  txt_h = cell_h_px - 2 * m
        row_h = txt_h / 3.0
        align = Qt.AlignLeft | Qt.AlignVCenter
        pt_v  = max(7, int(cell_h_mm * 0.50))
        pt_s  = max(6, int(cell_h_mm * 0.38))
        txt_w_used = cell_w_px - txt_x - m
        non_unknown = sample_type != 0
        w = QFont.Bold if non_unknown else QFont.Normal
        painter.setFont(QFont("Arial", pt_v, QFont.Bold))
        painter.setPen(QColor("#212121"))
        painter.drawText(QRectF(txt_x, txt_y,           txt_w_used, row_h), align, vial_label)
        painter.setFont(QFont("Arial", pt_s, w))
        painter.setPen(QColor("#212121") if non_unknown else QColor("#37474F"))
        painter.drawText(QRectF(txt_x, txt_y + row_h,   txt_w_used, row_h), align, sid)
        painter.setFont(QFont("Arial", pt_s, w))
        painter.setPen(QColor("#546E7A") if non_unknown else QColor("#78909C"))
        painter.drawText(QRectF(txt_x, txt_y + 2*row_h, txt_w_used, row_h), align, f"({aid})")
        return

    else:
        txt_y = m;  txt_h = cell_h_px - 2 * m
        row_h = txt_h / 3.0
        align = Qt.AlignHCenter | Qt.AlignVCenter
        if portrait:
            pt_v = max(8, int(cell_w_mm * 0.80))
            pt_s = max(7, int(cell_w_mm * 0.56))
        else:
            pt_v = max(8, int(cell_h_mm * 0.50))
            pt_s = max(7, int(cell_h_mm * 0.38))

    # shared text-draw path (portrait QR + text-only)
    non_unknown = sample_type != 0
    txt_w_used = cell_w_px - 2 * m
    painter.setFont(QFont("Arial", pt_v, QFont.Bold))
    painter.setPen(QColor("#212121"))
    painter.drawText(QRectF(m, txt_y,           txt_w_used, row_h), align, vial_label)
    w = QFont.Bold if non_unknown else QFont.Normal
    painter.setFont(QFont("Arial", pt_s, w))
    painter.setPen(QColor("#212121") if non_unknown else QColor("#37474F"))
    painter.drawText(QRectF(m, txt_y + row_h,   txt_w_used, row_h), align, sid)
    painter.setFont(QFont("Arial", pt_s, w))
    painter.setPen(QColor("#546E7A") if non_unknown else QColor("#78909C"))
    painter.drawText(QRectF(m, txt_y + 2*row_h, txt_w_used, row_h), align, f"({aid})")


# ── Tray grid page renderer ───────────────────────────────────────────────────

def render_tray_loadlist(printer: QPrinter, specs, tray_cfg, show_qr: bool, *,
                         type_map=None, run_id='', module_name='', equip_name='',
                         start_str='') -> int:
    """Render physical tray grid onto printer.  Returns total page count.
    All trays stacked on one page when cell height ≥ 12 mm; otherwise one tray per page."""
    traycount, trayrows, traycols = tray_cfg

    # Build lookup: (tray_num, pos_in_tray) → spec / sample_type
    spec_map  = {}
    _type_map = type_map or {}
    for spec in specs:
        ctn = dict(spec.lines).get("Container", "")
        if '-' in str(ctn):
            try:
                tn, pit = (int(p) for p in str(ctn).split('-', 1))
                spec_map[(tn, pit)] = spec
            except ValueError:
                pass

    # Only render trays that have at least one vial in spec_map
    occupied_trays = sorted({tn for (tn, pit) in spec_map})
    n_trays = len(occupied_trays)
    if n_trays == 0:
        return 0

    dpi      = printer.resolution()
    mm2px    = dpi / 25.4
    _s       = _AppSettings("YourOrg", "ISOTOPES_SUITE")
    mg_px    = float(_s.value("print/margin", 10.0)) * mm2px
    hdr_h_px = 14.0 * mm2px
    cgap_px  =  0.8 * mm2px
    tsep_px  =  6.0 * mm2px
    paper_mm = printer.paperSize(QPrinter.Millimeter)
    page_w   = paper_mm.width()  * mm2px
    page_h   = paper_mm.height() * mm2px
    avail_w  = page_w - 2 * mg_px
    avail_h  = page_h - 2 * mg_px - hdr_h_px

    cell_w_px = (avail_w - cgap_px * (traycols - 1)) / traycols
    cell_w_mm = cell_w_px / mm2px

    avail_for_cells = avail_h - tsep_px * (n_trays - 1) - cgap_px * n_trays * (trayrows - 1)
    cell_h_single   = avail_for_cells / (n_trays * trayrows)
    single_page     = (cell_h_single / mm2px) >= 12.0

    if single_page:
        cell_h_px   = cell_h_single
        total_pages = 1
    else:
        cell_h_px   = (avail_h - cgap_px * (trayrows - 1)) / trayrows
        total_pages = n_trays
    cell_h_mm = cell_h_px / mm2px

    mode_tag = "Labels" if show_qr else "Load List"
    tray_w   = traycols * cell_w_px + (traycols - 1) * cgap_px
    tray_h   = trayrows * cell_h_px + (trayrows - 1) * cgap_px

    def draw_page_header(tray_nums):
        hdr_y = mg_px * 0.35
        tray_note = (
            f"Trays {', '.join(str(t) for t in tray_nums)}  ({trayrows}×{traycols})"
            if len(tray_nums) > 1 else
            f"Tray {tray_nums[0]} / {traycount}  ({trayrows}×{traycols})"
        )
        painter.setFont(QFont("Arial", 9, QFont.Bold))
        painter.setPen(QColor("#37474F"))
        painter.drawText(
            QRectF(mg_px, hdr_y, avail_w * 0.72, hdr_h_px * 0.55),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"{module_name} Run {run_id}  ·  {mode_tag}  ·  {equip_name or '—'}"
            f"  ·  {start_str or '—'}",
        )
        painter.setPen(QColor("#4C82FF"))
        painter.drawText(
            QRectF(mg_px, hdr_y, avail_w, hdr_h_px * 0.55),
            Qt.AlignRight | Qt.AlignVCenter, tray_note,
        )
        rule_y = hdr_y + hdr_h_px * 0.65
        painter.setPen(QPen(QColor("#B0BEC5"), max(1.0, mm2px * 0.25)))
        painter.drawLine(QPointF(mg_px, rule_y), QPointF(page_w - mg_px, rule_y))

    def draw_tray(tray_num, origin_y, multi):
        sep_pen = QPen(QColor("#B0BEC5"), max(1.0, cgap_px * 0.5))
        painter.setPen(sep_pen)
        for col in range(1, traycols):
            sep_x = mg_px + col * (cell_w_px + cgap_px) - cgap_px / 2
            painter.drawLine(QPointF(sep_x, origin_y), QPointF(sep_x, origin_y + tray_h))
        for row in range(1, trayrows):
            sep_y = origin_y + row * (cell_h_px + cgap_px) - cgap_px / 2
            painter.drawLine(QPointF(mg_px, sep_y), QPointF(mg_px + tray_w, sep_y))

        for row in range(trayrows):
            for col in range(traycols):
                pit  = row * traycols + col + 1
                x    = mg_px + col * (cell_w_px + cgap_px)
                y    = origin_y + row * (cell_h_px + cgap_px)
                spec = spec_map.get((tray_num, pit))
                st   = _type_map.get((tray_num, pit), 0)
                painter.save()
                painter.translate(x, y)
                painter.setClipRect(QRectF(0, 0, cell_w_px, cell_h_px))
                draw_tray_cell(painter, cell_w_px, cell_h_px, cell_w_mm, cell_h_mm,
                               f"{tray_num}-{pit}", spec, show_qr, sample_type=st)
                painter.restore()

        painter.setPen(QPen(QColor("#546E7A"), max(2.0, mm2px * 0.40)))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(QRectF(mg_px, origin_y, tray_w, tray_h))

        if multi:
            badge_h  = min(cell_h_px * 0.25, 4.0 * mm2px)
            badge_pt = max(8, int(badge_h / mm2px * 2.0))
            painter.setFont(QFont("Arial", badge_pt, QFont.Bold))
            painter.setPen(QColor("#546E7A"))
            painter.drawText(
                QRectF(mg_px, origin_y - badge_h - mm2px, tray_w, badge_h),
                Qt.AlignCenter, f"TRAY {tray_num}",
            )

    painter = QPainter(printer)
    try:
        if single_page:
            draw_page_header(occupied_trays)
            grid_top = mg_px + hdr_h_px
            for i, tray_num in enumerate(occupied_trays):
                origin_y = grid_top + i * (tray_h + tsep_px)
                draw_tray(tray_num, origin_y, multi=(n_trays > 1))
        else:
            for i, tray_num in enumerate(occupied_trays):
                if i > 0:
                    printer.newPage()
                draw_page_header([tray_num])
                draw_tray(tray_num, mg_px + hdr_h_px, multi=False)
    finally:
        painter.end()
    return total_pages


# ── Default tray config ───────────────────────────────────────────────────────

def default_tray_cfg(specs, default_cols=8):
    """Derive (traycount, trayrows, traycols) from spec Container values.
    Used when no equipmenttrayconfig row exists for the instrument."""
    max_tn, max_pit = 1, 0
    for spec in specs:
        ctn = dict(spec.lines).get("Container", "")
        if '-' in str(ctn):
            try:
                tn, pit = (int(p) for p in str(ctn).split('-', 1))
                max_tn  = max(max_tn, tn)
                max_pit = max(max_pit, pit)
            except ValueError:
                pass
    traycols = default_cols
    trayrows = max(1, (max_pit + traycols - 1) // traycols)
    return (max_tn, trayrows, traycols)
