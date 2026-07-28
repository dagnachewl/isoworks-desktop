"""
plot_utils.py
=============
Shared matplotlib helpers for IsoWorks widgets.
"""
from __future__ import annotations

from typing import Callable, List, Tuple


def attach_hover_tooltip(canvas, ax, entries: List[Tuple]) -> None:
    """
    Attach a self-clearing hover annotation to one or more matplotlib artists.

    The tooltip appears when the mouse is over a data point and disappears
    immediately when the mouse leaves the artist or the figure — no stale
    yellow boxes left behind.

    Each call disconnects the previous hover callbacks registered on *canvas*
    so that redrawn figures don't accumulate stale handlers.

    Parameters
    ----------
    canvas : FigureCanvasQTAgg
    ax     : matplotlib Axes
    entries: list of (artist, label_fn)
        artist   – PathCollection (ax.scatter) or Line2D (ax.plot)
        label_fn – callable(point_index: int, x: float, y: float) -> str
    """
    # Disconnect any previous hover callbacks registered on this canvas.
    for cid in getattr(canvas, "_hover_tooltip_cids", []):
        try:
            canvas.mpl_disconnect(cid)
        except Exception:
            pass

    annot = ax.annotate(
        "", xy=(0, 0), xytext=(10, 10), textcoords="offset points",
        bbox=dict(boxstyle="round,pad=0.35", fc="#FFFDE7", ec="#F9A825",
                  alpha=0.95, lw=0.8),
        fontsize=7.5, zorder=20,
    )
    annot.set_visible(False)

    def _on_move(event):
        # Guard: skip if any tracked artist was orphaned after fig.clear()
        if any(getattr(a, "figure", None) is None for a, _ in entries):
            return
        if event.inaxes is not ax:
            if annot.get_visible():
                annot.set_visible(False)
                canvas.draw_idle()
            return
        hit = False
        for artist, fmt in entries:
            cont, ind = artist.contains(event)
            if cont and len(ind.get("ind", [])) > 0:
                idx = int(ind["ind"][0])
                try:
                    # PathCollection (scatter)
                    offs = artist.get_offsets()
                    x, y = float(offs[idx, 0]), float(offs[idx, 1])
                except Exception:
                    # Line2D
                    x = float(artist.get_xdata()[idx])
                    y = float(artist.get_ydata()[idx])
                annot.xy = (x, y)
                annot.set_text(fmt(idx, x, y))
                annot.set_visible(True)
                canvas.draw_idle()
                hit = True
                break
        if not hit and annot.get_visible():
            annot.set_visible(False)
            canvas.draw_idle()

    def _on_leave(event):
        if annot.get_visible():
            annot.set_visible(False)
            canvas.draw_idle()

    cid_move  = canvas.mpl_connect("motion_notify_event", _on_move)
    cid_leave = canvas.mpl_connect("figure_leave_event", _on_leave)
    canvas._hover_tooltip_cids = [cid_move, cid_leave]
