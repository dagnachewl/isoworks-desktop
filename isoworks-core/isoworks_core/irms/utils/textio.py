"""
irms/utils/textio.py — Robust text-file reading utilities for the irms subpackage.
Provides robust_read_table(), which attempts common separator and encoding
combinations to reliably parse CSV/TXT instrument exports into a DataFrame.
"""
# This file is part of your project and is intended to be distributed under the GPL-2 license.
# See the GNU General Public License version 2 for details.

from __future__ import annotations
import pandas as pd

def robust_read_table(path):
    """Try a few common separators/encodings and return a DataFrame."""
    try:
        df = pd.read_csv(path)
        if df.shape[1] > 1:
            return df
    except Exception as e:

        logging.warning(f"Exception caught: {e}")
    for sep in [",", "\t", ";", "|"]:
        for enc in [None, "utf-8", "latin-1", "utf-16"]:
            try:
                df = pd.read_csv(path, sep=sep, encoding=enc, engine="python")
                if df.shape[1] > 1:
                    return df
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); continue
    return pd.read_csv(path, engine="python", header=None)
