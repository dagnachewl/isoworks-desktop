"""
ui/tables.py — Table population utilities for the IsoWorks processor UI.
Provides update_data_table(), which clears and repopulates a QTableWidget
from a pandas DataFrame, handling None values and header alignment.
"""

# ui/tables.py
def update_data_table(table, data):
    import pandas as pd
    if table is None:
        return
    try:
        table.blockSignals(True)
        try:
            table.setSortingEnabled(False)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        table.clear()
        table.setRowCount(0)
        table.setColumnCount(0)

        if data is None or not isinstance(data, pd.DataFrame) or data.empty:
            return

        df = data.reset_index(drop=True).copy()
        df = df.where(pd.notnull(df), "")

        headers = [str(c) for c in df.columns]
        n_rows, n_cols = df.shape
        table.setColumnCount(n_cols)
        table.setRowCount(n_rows)
        table.setHorizontalHeaderLabels(headers)

        from PyQt5.QtWidgets import QTableWidgetItem
        for r in range(n_rows):
            for c in range(n_cols):
                val = df.iat[r, c]
                item = QTableWidgetItem("" if val is None else str(val))
                table.setItem(r, c, item)

        try:
            table.resizeColumnsToContents()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
    finally:
        try:
            table.setSortingEnabled(True)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try:
            table.blockSignals(False)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
