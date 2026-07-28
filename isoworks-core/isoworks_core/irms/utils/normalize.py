"""
irms/utils/normalize.py — Column normalisation utilities for dual-inlet DataFrames.
Provides normalize_dualinlet_df(), which flattens MultiIndex headers and applies
heuristic column renames to produce a consistent dual-inlet schema.
"""
from __future__ import annotations
import pandas as pd

def normalize_dualinlet_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [" ".join([str(x) for x in tup if pd.notna(x)]).strip() for tup in out.columns.values]
    out.columns = [str(c).strip() for c in out.columns]

    # Heuristic renames
    ren = {}
    for c in list(out.columns):
        lc = c.lower().strip()
        if lc in ("sample name","sample","sample_name","samplename"):
            renc = "sample_name"
        elif lc in ("ref. name","reference name","ref name","ref_name","refname","dual inlet ref  name:"):
            renc = "ref_name"
        elif lc in ("block","block no","blockno","block_nr","block number"):
            renc = "block"
        elif lc in ("cycle","step","scan","cycle nr"):
            renc = "cycle"
        elif lc in ("date","datetime","analysis time","date-time analyzed","time code"):
            renc = "timestamp"
        elif lc in ("identifier 1","identifier","id"):
            renc = "identifier_1"

    if ren:
        out = out.rename(columns=ren)

    # sample_id from identifier_1
    if "identifier_1" in out.columns and "sample_id" not in out.columns:
        out["sample_id"] = out["identifier_1"].astype(str).str.split("/").str[0].str.strip()

    # Alias isotope deltas
    def pick(colnames, patterns):
        for c in colnames:
            low = str(c).lower().replace(" ", "").replace("_","")
            for p in patterns:
                if low.startswith(p):
                    return c
        return None

    d[18] = pick(out.columns, ["d18o","δ18o","delta18o","delta18","d18o16","delta18o16"])
    dd  = pick(out.columns, ["dd","δd","delta2h","d2h","delta_dh","d_d","delta2h1h"])
    d[17] = pick(out.columns, ["d17o","δ17o","delta17o","delta17","d17o16","delta17o16"])
    if d[18] and "d18O" not in out.columns: out["d18O"] = pd.to_numeric(out[d[18]], errors="coerce")
    if dd  and "dD"   not in out.columns: out["dD"]   = pd.to_numeric(out[dd], errors="coerce")
    if d[17] and "d17O" not in out.columns: out["d17O"] = pd.to_numeric(out[d[17]], errors="coerce")

    # Coerce numerics
    for col in ["block","cycle"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")


    
    # Coalesce duplicate 'timestamp' columns (e.g., Date + Analysis Time)
    # After renaming, it's possible we have multiple columns named exactly 'timestamp'.
    # Selecting out['timestamp'] would yield a DataFrame, so we handle it explicitly.
    mask = [str(c) == 'timestamp' for c in out.columns]
    if any(mask):
        ts_df = out.loc[:, mask]
        # If more than one, combine left-to-right by first non-null after parsing to datetime
        if ts_df.shape[1] > 1:
            ser = None
            for i in range(ts_df.shape[1]):
                s = pd.to_datetime(ts_df.iloc[:, i], errors='coerce')
                ser = s if ser is None else ser.fillna(s)
            # drop all timestamp columns and replace with the coalesced one
            out = out.drop(columns=ts_df.columns, errors='ignore')
            out['timestamp'] = ser
        else:
            # single column named timestamp
            out['timestamp'] = pd.to_datetime(ts_df.iloc[:, 0], errors='coerce')


    return out
