"""
irms/readers/parse_thermo_dualinlet_xls.py — Thermo dual-inlet XLS normalisation helper.
Provides normalize_dualinlet_table() to flatten MultiIndex headers and apply
heuristic column renames for Thermo dual-inlet XLS/XLSX exports.
"""

import sys, re
import pandas as pd

# USAGE:
    # pip install 'xlrd<2.0'  # required for .xls
    # python parse_thermo_dualinlet_xls.py "HDO 26112014.xls"

def normalize_dualinlet_table(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [" ".join([str(x) for x in tup if x is not None]).strip() for tup in out.columns.values]
    out.columns = [str(c).strip() for c in out.columns]

    # map likely names
    ren = {}
    for c in list(out.columns):
        lc = c.lower().strip()
        if lc in ("sample name", "sample", "sample_name", "samplename"):
            renc = "sample_name"
        elif lc in ("ref. name", "reference name", "ref name", "ref_name"):
            renc = "ref_name"
        elif lc in ("block", "block no", "blockno", "block_nr", "block number"):
            renc = "block"
        elif lc in ("cycle", "step", "scan"):
            renc = "cycle"
    if ren:
        out = out.rename(columns=ren)

    # alias isotopes
    def pick(colnames, patterns):
        for c in colnames:
            low = str(c).lower().replace(" ", "").replace("_","")
            for p in patterns:
                if low.startswith(p):
                    return c
        return None

    d[18] = pick(out.columns, ["d18o", "δ18o", "delta18o", "delta18"])
    dd  = pick(out.columns, ["dd", "δd", "delta2h", "d2h", "delta_dh"])
    d[17] = pick(out.columns, ["d17o", "δ17o", "delta17o", "delta17"])
    if d[18] and "d18O" not in out.columns: out["d18O"] = pd.to_numeric(out[d[18]], errors="coerce")
    if dd  and "dD"   not in out.columns: out["dD"]   = pd.to_numeric(out[dd], errors="coerce")
    if d[17] and "d17O" not in out.columns: out["d17O"] = pd.to_numeric(out[d[17]], errors="coerce")

    for col in ["block", "cycle"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    return out

def main(p):
    # IMPORTANT: xlrd<2.0 is required for .xls
    # pip install "xlrd<2.0"
    df = pd.read_excel(p, engine="xlrd")
    norm = normalize_dualinlet_table(df)
    print("Parsed shape:", df.shape, "| Normalized shape:", norm.shape)
    print("Columns:", list(norm.columns))
    out_csv = p.rsplit(".", 1)[0] + "_normalized.csv"
    norm.to_csv(out_csv, index=False)
    print("Saved:", out_csv)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python parse_thermo_dualinlet_xls.py <file.xls>")
        sys.exit(2)
    main(sys.argv[1])
